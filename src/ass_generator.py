"""Bilingual ASS subtitle generation.

Produces a clean, player-friendly ASS file where each cue shows the original
line and the Chinese translation stacked together. Two styles (CN / ORG) are
defined; a mid-line `\\r` tag switches between them so both lines share one
Dialogue event and stay perfectly time-aligned.

The Chinese (primary) line sits on top by default; set cn_first=False to flip.

Style parameters live in the constants below — edit them to restyle every
generated file. The two fonts (and the two text colours) can also be overridden
per run from the GUI; the constants are the defaults used when nothing is set.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .subtitle_utils import SubtitleEntry

# --------------------------------------------------------------------- #
# Subtitle style parameters
# --------------------------------------------------------------------- #
# Canvas resolution. ASS players scale PlayRes to the actual video, so a small
# canvas (384x288 — the classic SD subtitle-group standard) keeps files tiny
# while looking identical at any resolution.
PLAY_RES_X = 384
PLAY_RES_Y = 288

# Fonts. PingFang SC is the macOS system Chinese font; on players that lack it
# (Windows / some TVs) the renderer falls back to its default CJK font.
# These are only the *defaults* — the GUI font picker overrides them per run.
CHS_FONT = "PingFang SC"
EN_FONT = "Arial"

# Font sizes, measured on the 384x288 canvas above.
CHS_SIZE = 20
EN_SIZE = 14

# Layout, scaled to the 384x288 canvas (roughly the same proportions as a
# 1920x1080 canvas with 20/40 margins and a 2px outline).
MARGIN_LR = 10                 # left / right safe margin
MARGIN_V = 10                  # bottom safe margin
CHS_OUTLINE, CHS_SHADOW = 1, 1
EN_OUTLINE, EN_SHADOW = 1, 1

# Colours are stored as plain hex (what the GUI colour picker produces) and
# converted to ASS byte order (&HAABBGGRR) by hex_to_ass_color().
CLR_CN = "#FFFFFF"          # white — Chinese (primary) line
CLR_EN = "#DDDDDD"          # light grey — original (secondary) line
CLR_OUTLINE = "#000000"     # black outline
CLR_BACK = "#000000"        # shadow colour (semi-transparent, see BACK_ALPHA)
BACK_ALPHA = "64"           # 0x64 ≈ 40% opacity

_HEX_DIGITS = set("0123456789abcdefABCDEF")


def hex_to_ass_color(value: str, alpha: str = "00") -> str:
    """Convert '#RRGGBB' (or 'RRGGBB' / 'RGB') to an ASS colour '&HAABBGGRR'.

    ASS stores colour channels in reverse (blue, green, red) order. Invalid
    input falls back to opaque white so a bad config can never break a file.
    """
    s = (value or "").strip().lstrip("#")
    if len(s) == 3:  # shorthand #abc -> #aabbcc
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or any(ch not in _HEX_DIGITS for ch in s):
        return "&H00FFFFFF"
    r, g, b = s[0:2], s[2:4], s[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def sanitize_font_name(value: Optional[str], default: str) -> str:
    """Return a font name that is safe to put in an ASS style line.

    ASS styles are a comma-separated field list, so a comma inside a family
    name would shift every following field and corrupt the style; a newline
    would break the line entirely. Anything unusable falls back to `default`.
    """
    name = " ".join((value or "").split())
    for bad in (",", ";", "\\"):
        name = name.replace(bad, " ")
    name = " ".join(name.split()).strip()
    return name or default


def _build_header(cn_color: Optional[str] = None,
                  en_color: Optional[str] = None,
                  cn_font: Optional[str] = None,
                  en_font: Optional[str] = None) -> str:
    """Assemble the [Script Info] + [V4+ Styles] header.

    `cn_color` / `en_color` are hex strings and `cn_font` / `en_font` are font
    family names, all as chosen in the GUI; None keeps the module defaults.
    """
    clr_cn = hex_to_ass_color(cn_color or CLR_CN)
    clr_en = hex_to_ass_color(en_color or CLR_EN)
    clr_outline = hex_to_ass_color(CLR_OUTLINE)
    clr_back = hex_to_ass_color(CLR_BACK, alpha=BACK_ALPHA)
    fam_cn = sanitize_font_name(cn_font, CHS_FONT)
    fam_en = sanitize_font_name(en_font, EN_FONT)

    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )

    def style(name: str, font: str, size: int, colour: str,
              outline: float, shadow: float) -> str:
        return (
            f"Style: {name},{font},{size},{colour},{colour},{clr_outline},"
            f"{clr_back},0,0,0,0,100,100,0,0,1,{outline},{shadow},"
            f"2,{MARGIN_LR},{MARGIN_LR},{MARGIN_V},1"
        )

    return (
        "[Script Info]\n"
        "Title: Bilingual Subtitle\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_RES_X}\n"
        f"PlayResY: {PLAY_RES_Y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        f"{style_format}\n"
        f"{style('Default', fam_cn, CHS_SIZE, clr_cn, CHS_OUTLINE, CHS_SHADOW)}\n"
        f"{style('CN', fam_cn, CHS_SIZE, clr_cn, CHS_OUTLINE, CHS_SHADOW)}\n"
        f"{style('ORG', fam_en, EN_SIZE, clr_en, EN_OUTLINE, EN_SHADOW)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginV, MarginR, "
        "Effect, Text\n"
    )


ASS_HEADER = _build_header()


def _srt_time_to_ass(timestamp: str) -> str:
    """Convert 'HH:MM:SS,mmm' (SRT) to 'H:MM:SS.cc' (ASS centiseconds)."""
    t = timestamp.strip().replace(".", ",")
    try:
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
    except ValueError:
        return "0:00:00.00"
    centis = int((ms + "000")[:3]) // 10
    return f"{int(h)}:{m}:{s}.{centis:02d}"


def resolve_output_name(template: str, original_stem: str, source_code: str) -> str:
    """Resolve a naming template using safe token replacement.

    Supports {original_name} and {source_lang}; any other literal text in the
    template is preserved as-is (avoids str.format KeyError on stray braces).
    """
    return (
        template.replace("{original_name}", original_stem)
        .replace("{source_lang}", source_code)
    )


def generate_ass(
    entries: list[SubtitleEntry],
    chinese_texts: list[str],
    output_path: Path,
    cn_first: bool = True,
    cn_color: Optional[str] = None,
    en_color: Optional[str] = None,
    cn_font: Optional[str] = None,
    en_font: Optional[str] = None,
) -> Path:
    """Write a bilingual ASS file. Returns the output path.

    `cn_color` / `en_color` are hex strings ('#RRGGBB') and `cn_font` /
    `en_font` are font family names; None keeps the defaults.
    """
    header = _build_header(cn_color, en_color, cn_font, en_font)
    lines = [header.rstrip("\n")]
    for entry, cn in zip(entries, chinese_texts):
        start = _srt_time_to_ass(entry.start)
        end = _srt_time_to_ass(entry.end)
        org = entry.text.replace("\n", "\\N")
        cn_line = (cn or "").replace("\n", "\\N")

        if cn_first:
            # Chinese on top (CN style), original below (ORG style).
            text = f"{{\\rCN}}{cn_line}{{\\rORG}}\\N{org}"
        else:
            # Original on top, Chinese below.
            text = f"{{\\rORG}}{org}{{\\rCN}}\\N{cn_line}"

        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    out = Path(output_path)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
