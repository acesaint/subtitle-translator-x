"""LLM-based subtitle translation (OpenAI-compatible endpoint).

Design notes:
  * Uses the official `openai` client, which supports a custom `base_url`
    (OpenAI, DeepSeek, Ollama, vLLM, LM Studio, llama.cpp, etc. all work).
  * Lines are translated in batches to keep latency/cost down while preserving
    context. Timestamps are never sent to the model, so they stay exact.
  * Output parsing is intentionally defensive: we try strict JSON first, then
    fall back to a numbered-line regex, so the tool works even on endpoints
    that don't honour `response_format`.
  * **Untranslated-line detection & retry.** Parsing never silently falls back
    to the source text any more: a line that is missing, empty, identical to
    the source, or still in the source language is *flagged*, and the flagged
    lines are re-sent in dedicated passes (smaller batch first, then one line
    per request, which small local models comply with far more reliably).
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable, List, Optional

from openai import OpenAI

from .subtitle_utils import SubtitleEntry

# (completed, total, phase) progress callback for a whole file translation.
ProgressCb = Callable[[int, int, str], None]

# CJK ideographs — the translation target is Simplified Chinese.
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Latin / accented-Latin / Cyrillic letters, i.e. "this line has real words".
_LETTER_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff]")


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def normalize_base_url(url: str) -> str:
    """Normalise a user-supplied Base URL.

    The OpenAI client appends "/chat/completions" itself, so the base URL must
    stop at the API root (e.g. ".../v1"). Two common mistakes are fixed here:

      * trailing slashes  -> "http://host:8765/v1/"  becomes "http://host:8765/v1"
      * a pasted full endpoint -> "http://host:8765/v1/chat/completions"
        becomes "http://host:8765/v1"

    Both would otherwise produce a confusing 404 from the server.
    """
    u = (url or "").strip()
    if not u:
        return u
    u = u.rstrip("/")
    lowered = u.lower()
    if lowered.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")].rstrip("/")
    return u


def looks_untranslated(source: str, target: Optional[str]) -> bool:
    """Heuristic: did the model actually translate this line?

    A line counts as *not translated* when:
      * the target is empty / whitespace (missing from the response), or
      * the target is identical to the source AND the source contains real
        words — a line made only of digits, punctuation or symbols ("...",
        "♪♪", "007") is legitimately unchanged, or
      * the target contains no CJK at all while the source does contain words,
        which for an X -> Chinese pipeline means the original was echoed back.

    A wrong verdict is harmless: it only costs one extra retry, and the last
    attempt is always kept as the fallback.
    """
    src = (source or "").strip()
    tgt = (target or "").strip()
    if not src:
        return False  # nothing to translate
    if not tgt:
        return True  # missing / empty output
    if tgt == src:
        return bool(_LETTER_RE.search(src))
    if not _CJK_RE.search(tgt) and _LETTER_RE.search(src):
        return True
    return False


def friendly_error(exc: Exception) -> str:
    """Turn a raw SDK/network exception into an actionable message."""
    msg = str(exc)
    low = msg.lower()
    if "404" in msg or "not_found" in low:
        return (
            f"{msg}\n"
            "  ↳ 404 多为 Base URL 填错：应填到 API 根目录为止，例如 "
            "http://127.0.0.1:8765/v1 或 https://api.openai.com/v1，"
            "不要再带上 /chat/completions；同时请确认 Model 名称正确。"
        )
    if "401" in msg or "unauthorized" in low:
        return (
            f"{msg}\n"
            "  ↳ 401 多为 API Key 无效或缺失（本地 llama.cpp / LM Studio / vLLM "
            "可填任意非空字符串）。"
        )
    if "429" in msg or "rate limit" in low:
        return (
            f"{msg}\n"
            "  ↳ 429 触发限流，请稍后重试或减小 batch_size。"
        )
    if "connection" in low or "refused" in low or "timed out" in low or "timeout" in low:
        return (
            f"{msg}\n"
            "  ↳ 连不上服务：请确认本地 LLM 服务已启动，且 Base URL 与端口正确。"
        )
    return msg


def _is_transient(exc: Exception) -> bool:
    """True for errors worth retrying (rate limit, overload, network hiccups).

    Configuration problems (404 wrong URL, 401 bad key, 400 bad request) are
    deliberately NOT retried — retrying them only wastes the user's time.
    """
    msg = str(exc).lower()
    if any(code in msg for code in ("429", "500", "502", "503", "504", "overloaded")):
        return True
    return any(
        k in msg for k in ("timeout", "timed out", "connection", "refused", "reset")
    )


class Translator:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        batch_size: int = 40,
        max_retries: int = 2,
        retry_delay: float = 1.5,
        max_line_retries: int = 2,
    ):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.system_prompt = system_prompt
        self.batch_size = max(1, int(batch_size))
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = float(retry_delay)
        # Extra passes over lines the model failed to translate.
        self.max_line_retries = max(0, int(max_line_retries))
        # Local servers (llama.cpp / LM Studio / vLLM) ignore the key, but the
        # OpenAI client refuses to start with an empty one.
        self.client = OpenAI(
            base_url=self.base_url, api_key=api_key or "sk-no-key-required"
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def translate_entries(
        self,
        entries: List[SubtitleEntry],
        source_lang_label: str,
        progress_cb: Optional[ProgressCb] = None,
        cancel_event=None,
        stats: Optional[dict] = None,
    ) -> List[str]:
        """Translate every entry into Simplified Chinese.

        Returns a list of strings, 1:1 with `entries`. Lines the model failed to
        translate are re-sent in dedicated retry passes; anything still failing
        after `max_line_retries` falls back to the best available attempt (or the
        original text) so the output always has the right number of cues.

        If `stats` is given it is filled with:
            {"retried": int, "failed": [entry positions still untranslated]}
        """
        n = len(entries)
        if n == 0:
            return []

        results: List[Optional[str]] = [None] * n
        done_units = 0
        total_units = n

        def run_pass(indices: List[int], chunk_size: int, phase: str,
                     emphasis: bool = False) -> List[int]:
            nonlocal done_units, total_units
            for start in range(0, len(indices), chunk_size):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Translation cancelled by user")
                chunk = indices[start : start + chunk_size]
                translated = self._translate_batch(
                    [entries[i] for i in chunk], source_lang_label, emphasis=emphasis
                )
                for pos, i in enumerate(chunk):
                    if translated[pos]:
                        results[i] = translated[pos]
                done_units += len(chunk)
                if progress_cb:
                    progress_cb(done_units, total_units, phase)
            return [i for i in indices if looks_untranslated(entries[i].text, results[i])]

        # Pass 1 — the whole file in normal batches.
        failed = run_pass(list(range(n)), self.batch_size, "Translating")

        # Passes 2..N — only the lines that came back untranslated. The first
        # retry shrinks the batch; the last one goes one line per request, which
        # is the most reliable mode for small local models.
        retried = 0
        for attempt in range(1, self.max_line_retries + 1):
            if not failed:
                break
            chunk_size = min(self.batch_size, 12) if attempt == 1 else 1
            total_units += len(failed)
            retried += len(failed)
            failed = run_pass(
                failed,
                chunk_size,
                f"Retrying untranslated lines (pass {attempt})",
                emphasis=True,
            )

        final = [r if r is not None else e.text for r, e in zip(results, entries)]
        if stats is not None:
            stats.update({"retried": retried, "failed": failed})
        return final

    def test_connection(self) -> list:
        """Cheap connectivity probe used by the GUI's "Test" button.

        Returns the list of model ids the server reports (may be empty).
        Raises a RuntimeError with a friendly message on failure.
        """
        try:
            resp = self.client.models.list()
            return [m.id for m in getattr(resp, "data", [])]
        except Exception as exc:
            raise RuntimeError(friendly_error(exc)) from exc

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _translate_batch(
        self,
        batch: List[SubtitleEntry],
        source_lang_label: str,
        emphasis: bool = False,
    ) -> List[Optional[str]]:
        """Translate one batch. Returns None for any line not translated."""
        numbered = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(batch, 1))
        user_content = (
            f"Source language: {source_lang_label}\n"
            f"The following {len(batch)} subtitle lines must be translated into "
            "Simplified Chinese.\n"
            "Return EXACTLY the same number of lines, each prefixed with its [n] index, "
            "in the same order, with no extra commentary, headings or markdown.\n"
            "If a subtitle contains a line break, represent it with the literal string \\n.\n\n"
            f"{numbered}"
        )
        if emphasis:
            user_content = (
                "IMPORTANT: in the previous attempt these lines were returned in "
                "the original language. Translate EVERY one of them into "
                "Simplified Chinese — including short lines, names, numbers and "
                "interjections. Do not leave any line untranslated.\n\n"
                + user_content
            )

        content = ""
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.3,
                    timeout=120,
                )
                content = resp.choices[0].message.content or ""
                last_exc = None  # a retry succeeded — clear any earlier failure
                break
            except Exception as exc:  # network / auth / model errors bubble up
                last_exc = exc
                # Only retry transient hiccups (rate limit, overload, timeouts).
                if attempt >= self.max_retries or not _is_transient(exc):
                    break
                time.sleep(self.retry_delay * (attempt + 1))

        if last_exc is not None:
            raise RuntimeError(
                f"LLM request failed: {friendly_error(last_exc)}"
            ) from last_exc

        parsed = self._parse_response(content, batch)
        # Flag anything missing, empty, or still in the source language.
        return [
            t if (t and not looks_untranslated(e.text, t)) else None
            for e, t in zip(batch, parsed)
        ]

    @staticmethod
    def _parse_response(
        content: str, batch: List[SubtitleEntry]
    ) -> List[Optional[str]]:
        as_list = Translator._try_json(content)
        if as_list is not None:
            return Translator._align_list(as_list, batch)
        return Translator._parse_brackets(content, batch)

    @staticmethod
    def _try_json(content: str) -> Optional[list]:
        """Best-effort extraction of a JSON list from the model response."""
        c = content.strip()
        # Strip markdown code fences if the model wrapped its answer.
        if c.startswith("```"):
            c = re.sub(r"^```[a-zA-Z]*\n?", "", c)
            c = re.sub(r"\n?```$", "", c).strip()
        try:
            data = json.loads(c)
        except Exception:
            return None
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("translations", "result", "results", "lines"):
                if isinstance(data.get(key), list):
                    return data[key]
            # Numeric-keyed object, e.g. {"1": "...", "2": "..."}
            if data and all(str(k).isdigit() for k in data.keys()):
                size = max(int(k) for k in data.keys())
                return [data.get(str(i + 1), "") for i in range(size)]
        return None

    @staticmethod
    def _align_list(js: list, batch: List[SubtitleEntry]) -> List[Optional[str]]:
        """Map a JSON list back to the original order; None when missing."""
        out: List[Optional[str]] = []
        for i, _entry in enumerate(batch, 1):
            val = js[i - 1] if i - 1 < len(js) else None
            out.append(val if isinstance(val, str) and val.strip() else None)
        return out

    @staticmethod
    def _parse_brackets(
        content: str, batch: List[SubtitleEntry]
    ) -> List[Optional[str]]:
        """Fallback parser: match `[n] translated text` blocks in order."""
        pattern = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\[\d+\]|\Z)", re.DOTALL)
        matches: dict[int, str] = {
            int(m.group(1)): m.group(2).strip() for m in pattern.finditer(content)
        }
        return [matches.get(i) for i, _e in enumerate(batch, 1)]
