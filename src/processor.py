"""Pipeline orchestrator: extract -> translate -> ASS, for a whole folder.

`Processor.run` iterates every video in the input folder, drives the
extractor/translator/ass_generator, and reports progress through callbacks so
the GUI can stay responsive. Long-running work is expected to be invoked from a
background thread (see gui.py); cancellation is cooperative via `cancel_event`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .extractor import (
    VIDEO_EXTENSIONS,
    list_subtitle_streams,
    select_stream,
    extract_subtitle,
)
from .translator import Translator
from .subtitle_utils import parse_srt
from .ass_generator import generate_ass, resolve_output_name

LogCb = Callable[[str], None]
ProgressCb = Callable[[float, str], None]


class Processor:
    def __init__(
        self,
        *,
        video_folder: Path,
        output_folder: Path,
        source_lang: str,
        source_lang_label: str,
        naming_format: str,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        overwrite: bool,
        keep_intermediate: bool,
        skip_existing_chinese: bool,
        cn_first: bool,
        batch_size: int = 40,
        max_line_retries: int = 2,
        cn_color: str = "",
        en_color: str = "",
        cn_font: str = "",
        en_font: str = "",
    ):
        self.video_folder = Path(video_folder)
        self.output_folder = Path(output_folder)
        self.source_lang = source_lang
        self.source_lang_label = source_lang_label
        self.naming_format = naming_format
        self.overwrite = overwrite
        self.keep_intermediate = keep_intermediate
        self.skip_existing_chinese = skip_existing_chinese
        self.cn_first = cn_first
        # Subtitle text colours (hex); empty string keeps the generator default.
        self.cn_color = cn_color
        self.en_color = en_color
        # Subtitle font families; empty string keeps the generator default.
        self.cn_font = cn_font
        self.en_font = en_font
        # Build the LLM client once and reuse it across all files.
        self.translator = Translator(
            base_url=base_url, api_key=api_key, model=model,
            system_prompt=system_prompt, batch_size=batch_size,
            max_line_retries=max_line_retries,
        )

    def run(
        self,
        log_cb: LogCb,
        progress_cb: ProgressCb,
        cancel_event,
    ) -> str:
        video_files = sorted(
            p for p in self.video_folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        )
        if not video_files:
            msg = "No video files found in the selected folder."
            log_cb(msg)
            return msg

        log_cb(f"Found {len(video_files)} video file(s).")
        total = len(video_files)
        ok = failed = 0

        for fi, vf in enumerate(video_files):
            if cancel_event.is_set():
                log_cb("Cancellation requested — stopping.")
                break
            log_cb(f"[{fi + 1}/{total}] Processing: {vf.name}")
            try:
                self._process_one(vf, fi, total, log_cb, progress_cb, cancel_event)
                ok += 1
            except InterruptedError:
                raise
            except Exception as exc:
                log_cb(f"  -> Skipped due to error: {exc}")
                failed += 1

        summary = f"Finished. Success: {ok}, Failed/Skipped: {failed}, Total: {total}"
        return summary

    # ------------------------------------------------------------------ #
    def _process_one(self, vf, fi, total, log_cb, progress_cb, cancel_event):
        # 1) Probe subtitle streams.
        streams = list_subtitle_streams(vf)
        text_streams = [s for s in streams if s["is_text"]]
        if not text_streams:
            raise RuntimeError("no text-based subtitle track found")

        if self.skip_existing_chinese and any(
            s["language"] in ("chi", "zho", "zh") for s in text_streams
        ):
            log_cb("  -> Already has Chinese subtitle; skipping (option enabled).")
            return

        target = select_stream(text_streams, self.source_lang)
        if not target:
            raise RuntimeError(
                f"no subtitle matching language '{self.source_lang}' found"
            )
        log_cb(
            f"  -> Using stream #{target['index']} "
            f"({target['codec']}, lang={target['language']})"
        )

        # 2) Resolve output path and honour overwrite setting.
        out_name = resolve_output_name(self.naming_format, vf.stem, self.source_lang)
        out_path = self.output_folder / out_name
        if out_path.exists() and not self.overwrite:
            log_cb(f"  -> Output exists, skipping (overwrite off): {out_path.name}")
            return

        # 3) Extract to a temporary SRT next to the output folder.
        tmp_srt = self.output_folder / f"._{vf.stem}.tmp.srt"
        log_cb("  -> Extracting subtitle...")
        extract_subtitle(vf, target["index"], tmp_srt, overwrite=True)
        if not tmp_srt.exists() or tmp_srt.stat().st_size == 0:
            tmp_srt.unlink(missing_ok=True)
            raise RuntimeError("extraction produced an empty file")

        try:
            entries = parse_srt(tmp_srt.read_text(encoding="utf-8", errors="ignore"))
            if not entries:
                raise RuntimeError("extracted subtitle has no usable lines")
            log_cb(f"  -> Extracted {len(entries)} line(s).")

            # 4) Translate in batches, reporting fine-grained progress.
            #    Pass 1 occupies 0..0.85 of this file; retry passes fill the rest,
            #    and the bar never moves backwards.
            state = {"frac": 0.0}

            def _on_batch(done, tot, phase):
                within = (done / tot) if tot else 1.0
                if phase.startswith("Translating"):
                    overall = 0.85 * within
                else:
                    overall = 0.85 + 0.15 * within
                state["frac"] = max(state["frac"], overall)
                progress_cb((fi + state["frac"]) / total,
                            f"{phase} — {vf.name}: {done}/{tot}")

            log_cb("  -> Translating...")
            stats: dict = {}
            chinese = self.translator.translate_entries(
                entries, self.source_lang_label,
                progress_cb=_on_batch, cancel_event=cancel_event, stats=stats,
            )

            if stats.get("retried"):
                log_cb(
                    f"  -> 检测到 {stats['retried']} 行未翻译/翻译失败，已自动补翻。"
                )
            if stats.get("failed"):
                bad = stats["failed"]
                preview = "; ".join(
                    f"#{entries[i].index} {entries[i].text[:30]!r}"
                    for i in bad[:5]
                )
                more = f" ...(共 {len(bad)} 行)" if len(bad) > 5 else ""
                log_cb(
                    f"  ⚠️ 仍有 {len(bad)} 行未能译成中文（已保留原文），"
                    f"请手动检查：{preview}{more}"
                )

            progress_cb((fi + 1) / total, f"Generating ASS: {vf.name}")

            # 5) Write the bilingual ASS file.
            generate_ass(
                entries, chinese, out_path,
                cn_first=self.cn_first,
                cn_color=self.cn_color, en_color=self.en_color,
                cn_font=self.cn_font, en_font=self.en_font,
            )
            log_cb(f"  -> Saved: {out_path.name}")
        finally:
            # Clean up the temp SRT unless the user wants to keep intermediates.
            if not self.keep_intermediate:
                tmp_srt.unlink(missing_ok=True)
            else:
                kept = self.output_folder / f"{vf.stem}.{self.source_lang}.srt"
                try:
                    tmp_srt.rename(kept)
                except Exception:
                    pass  # best-effort only
