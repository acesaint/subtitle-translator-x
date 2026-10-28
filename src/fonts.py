"""Font discovery for the subtitle font pickers.

Tk can enumerate the families the system provides, but it cannot report which
glyphs a family actually contains. `is_cjk_font()` therefore uses a curated
name list instead of a real glyph probe: macOS ships its Chinese fonts under
stable names (PingFang SC, Songti SC, …), and the list also covers the common
Linux / Windows CJK families, so a config written on one machine still gets
sensible hints on another.

The family list is cached for the lifetime of the process — enumerating fonts
costs a few dozen milliseconds and the set only changes when the user installs
a font, which requires an app restart anyway.
"""
from __future__ import annotations

from functools import lru_cache

import tkinter.font as tkfont

# Tk's own alias names are not real families; showing them would be noise.
_TK_INTERNAL = {
    "tkdefaultfont", "tktextfont", "tkfixedfont", "tkheadingfont",
    "tkcaptionfont", "tkmenufont", "tksmallcaptionfont", "tktooltipfont",
    "tkiconfont",
}

# Substrings that identify families with Chinese (or at least Han) coverage.
_CJK_KEYWORDS = (
    # macOS / iOS Chinese families
    "pingfang", "hiragino", "heiti", "songti", "kaiti", "yuanti",
    "hannotate", "hanzipen", "lantinghei", "weibei", "xingkai", "wawati",
    "baoli", "libian", "yuppy", "biaukai", "ligothic", "lisung",
    "hanhei", "stheiti", "stxihei", "stsong", "stkaiti", "stfangsong",
    "stzhongsong",
    # Noto / Source Han / community CJK families
    "noto sans cjk", "noto serif cjk", "noto sans sc", "noto serif sc",
    "noto sans tc", "source han", "sarasa", "lxgw", "wenquanyi",
    "droid sans fallback",
    # Windows
    "yahei", "simhei", "simsun", "nsimsun", "fangsong", "mingliu",
    "jhenghei", "ms gothic", "ms mincho", "meiryo", "malgun", "batang",
    "gulim", "dotum", "nanum",
    # Japanese / Korean families that carry Han glyphs
    "yu gothic", "yu mincho", "yugothic", "yumincho", "toppan", "klee",
    "tsukushi", "gothic", "mincho",
    # Broad-coverage fallbacks
    "unicode ms", "ar pl", "ukai", "uming", "dfkai",
)


@lru_cache(maxsize=1)
def list_font_families() -> tuple[str, ...]:
    """Every font family this Tk build can render with, alphabetically sorted.

    Returns an empty tuple when no Tk root exists yet (Tk cannot answer before
    the interpreter is up) — callers treat that as "no fonts to offer".
    """
    try:
        families = tkfont.families()
    except Exception:
        return ()

    cleaned: set[str] = set()
    for raw in families:
        name = (raw or "").strip()
        # macOS exposes vertical variants as "@FontName" and hides some system
        # faces behind a leading dot; neither can be used in an ASS file.
        if not name or name.startswith((".", "@")):
            continue
        if name.lower() in _TK_INTERNAL:
            continue
        cleaned.add(name)
    return tuple(sorted(cleaned, key=str.lower))


def is_cjk_font(family: str) -> bool:
    """Best-effort guess: does `family` contain Chinese glyphs?"""
    name = (family or "").strip().lower()
    return bool(name) and any(key in name for key in _CJK_KEYWORDS)


def is_installed(family: str) -> bool:
    """True when `family` is among the families Tk can actually use."""
    name = (family or "").strip().lower()
    if not name:
        return False
    return any(f.lower() == name for f in list_font_families())
