"""Shared helpers for subtitle text parsing.

Parses SRT content into structured entries. ASS generation consumes these
entries and reuses their original timestamps so timing is never altered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SubtitleEntry:
    index: int
    start: str  # original SRT timestamp, e.g. "00:00:01,000"
    end: str
    text: str


# Matches an SRT timestamp pair: "00:00:01,000 --> 00:00:04,000"
_TIMING_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"
)


def parse_srt(content: str) -> list[SubtitleEntry]:
    """Parse raw SRT text into a list of SubtitleEntry objects.

    Robust against Windows/Unix line endings, an optional leading index line,
    and multi-line subtitle text blocks.
    """
    content = content.replace("\r\n", "\n").strip()
    if not content:
        return []

    entries: list[SubtitleEntry] = []
    # Split into blocks separated by one or more blank lines.
    for block in re.split(r"\n\s*\n", content):
        lines = block.split("\n")
        timing_idx = None
        for i, line in enumerate(lines):
            if "-->" in line:
                timing_idx = i
                break
        if timing_idx is None:
            continue

        m = _TIMING_RE.search(lines[timing_idx])
        if not m:
            continue

        start, end = m.group(1), m.group(2)
        text = "\n".join(lines[timing_idx + 1:]).strip()
        if not text:
            continue

        entries.append(
            SubtitleEntry(index=len(entries) + 1, start=start, end=end, text=text)
        )
    return entries
