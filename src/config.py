"""Configuration management for Subtitle Translator.

Loads/saves user preferences (folders, LLM settings, naming format, options)
to a JSON file in the user's home directory so they persist between runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ass_generator import CHS_FONT as DEFAULT_CN_FONT
from .ass_generator import CLR_CN as DEFAULT_CN_COLOR
from .ass_generator import CLR_EN as DEFAULT_EN_COLOR
from .ass_generator import EN_FONT as DEFAULT_EN_FONT

# Where the config file lives. Kept in the user home so it survives across runs.
DEFAULT_CONFIG_PATH = Path.home() / ".subtitle_translator.json"

# Default system prompt for the translation LLM. Encourages natural, subtitle-style
# Simplified Chinese and enforces the strict output format the parser relies on.
DEFAULT_SYSTEM_PROMPT = (
    "You are a professional subtitle translator. Translate the provided subtitle "
    "lines into fluent, natural Simplified Chinese suitable for on-screen viewing.\n"
    "Rules:\n"
    "1. Keep proper nouns, technical terms, character names and brand names consistent.\n"
    "2. Do not over-translate or add explanations; stay concise like real subtitles.\n"
    "3. Preserve the meaning, tone and context of the original.\n"
    "4. Output EXACTLY one translated line per input line, prefixed with the same [n] index.\n"
    "5. Do not add any extra commentary, headings or markdown formatting."
)

# Source-language dropdown: (display name, short code used in naming/output)
DEFAULT_LANGUAGE_OPTIONS = [
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Chinese", "zh"),
    ("French", "fr"),
    ("German", "de"),
    ("Spanish", "es"),
    ("Auto-detect", "auto"),
]

# Built-in output naming presets shown in the GUI combobox.
NAMING_PRESETS = [
    "{original_name}.zh.ass",
    "{original_name}.en.zh.ass",
    "{original_name}.{source_lang}.zh.ass",
    "{original_name}_bilingual.ass",
]

# Sensible defaults for every setting. Unknown keys loaded from disk are preserved.
DEFAULT_CONFIG: dict[str, Any] = {
    "video_folder": "",
    "output_folder": "",
    "source_language": "en",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model_name": "gpt-4o-mini",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "naming_format": NAMING_PRESETS[0],
    "overwrite_existing": False,
    "keep_intermediate": False,
    "skip_existing_chinese": True,
    "cn_first": True,
    "batch_size": 40,
    # Extra passes over lines the model failed to translate (0 = disabled).
    "max_line_retries": 2,
    # Subtitle text colours (hex, picked in the GUI).
    "cn_color": DEFAULT_CN_COLOR,
    "en_color": DEFAULT_EN_COLOR,
    # Subtitle font families (picked in the GUI from installed fonts).
    "cn_font": DEFAULT_CN_FONT,
    "en_font": DEFAULT_EN_FONT,
    # Last window geometry as "WxH+X+Y"; restored when it still fits the screen.
    "window_geometry": "",
    # "System" | "Dark" | "Light" — the header switcher remembers the choice.
    "appearance_mode": "System",
}


class AppConfig:
    """Thin wrapper around a JSON config file with dict-like access."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH):
        self.path = Path(path)
        self.data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
        except Exception as exc:  # never crash the app over a corrupt config
            print(f"[config] Warning: could not load config ({exc}); using defaults.")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[config] Warning: could not save config ({exc}).")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
