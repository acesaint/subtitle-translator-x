"""Subtitle extraction using ffmpeg / ffprobe.

Responsibilities:
  * Probe a video for its subtitle streams.
  * Pick the best text-based stream matching the requested language.
  * Extract that stream to a temporary SRT file (ffmpeg normalises the format).

Image-based subtitles (e.g. PGS/dvbsub) are explicitly NOT supported in v1 —
the tool logs a clear warning and skips the file instead of attempting OCR.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

# Video container extensions we will look inside.
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".wmv", ".m4v", ".ts", ".m2ts",
}

# Subtitle codecs we treat as text (translatable). Image codecs are excluded.
TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text",
    "webvtt", "text", "ssa", "teletext",
}

# Map our short language code -> the 3-letter code ffmpeg/ffprobe reports.
FFMPEG_LANG_MAP = {
    "en": "eng", "ja": "jpn", "ko": "kor", "zh": "chi",
    "fr": "fre", "de": "ger", "es": "spa", "auto": None,
}

# Chinese can be reported as chi / zho / zh depending on the muxer.
CHINESE_CODES = {"chi", "zho", "zh"}


def list_subtitle_streams(video_path: Path) -> list[dict]:
    """Return a list of subtitle streams with index / codec / language / is_text."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "s", str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe not found. Install ffmpeg via Homebrew: brew install ffmpeg"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe timed out while probing the video.") from exc

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[-300:]}")

    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned non-JSON output.") from exc

    streams: list[dict] = []
    for s in info.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        codec = (s.get("codec_name") or "").lower()
        lang = (
            s.get("tags", {}).get("language")
            or s.get("language")
            or "und"
        ).lower()
        streams.append(
            {
                "index": s["index"],
                "codec": codec,
                "language": lang,
                "is_text": codec in TEXT_CODECS,
            }
        )
    return streams


def select_stream(streams: list[dict], lang_code: str) -> Optional[dict]:
    """Choose the best matching text stream for the requested language.

    - For "auto": return the first text-based stream.
    - Otherwise: exact language match first, then a 2-letter prefix match
      (e.g. "chi" vs "zh"), so we're forgiving about muxer metadata.
    """
    text_streams = [s for s in streams if s["is_text"]]
    if not text_streams:
        return None
    if lang_code == "auto":
        return text_streams[0]

    code = FFMPEG_LANG_MAP.get(lang_code, lang_code)
    candidates = {code}
    if lang_code == "zh":
        candidates |= CHINESE_CODES

    for s in text_streams:
        if s["language"] in candidates:
            return s
    # Fallback: prefix match (e.g. target "eng" matches "eng", "en").
    for s in text_streams:
        if str(code) and s["language"].startswith(str(code)[:2]):
            return s
    return None


def extract_subtitle(
    video_path: Path, stream_index: int, out_srt: Path, overwrite: bool = True
) -> Path:
    """Extract one subtitle stream to a normalized SRT file.

    Uses the absolute stream specifier `0:<index>` so the correct track is
    pulled regardless of how many subtitle streams the file contains.
    """
    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-i", str(video_path),
        "-map", f"0:{stream_index}",
        "-c:s", "srt",        # normalise everything to SRT for reliable parsing
        str(out_srt),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg via Homebrew: brew install ffmpeg"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg extraction timed out.") from exc

    if result.returncode != 0 or not out_srt.exists():
        # Surface the tail of stderr — ffmpeg is usually explicit about why.
        raise RuntimeError(f"ffmpeg extraction failed: {result.stderr.strip()[-400:]}")
    return out_srt
