"""Graphical user interface for Subtitle Translator.

Built with CustomTkinter for a native-feeling, themeable (dark/light) UI on
macOS. All heavy pipeline work runs in a background thread; UI updates are
scheduled back onto the main thread via `after(0, ...)` so the window never
freezes.

Layout notes: every section is a card with a header row and a *separate* inner
content frame, so section titles can never collide with the first row of
controls (an earlier version gridded both at row 0).

Scrolling: the whole page lives in a `CTkScrollableFrame` driven by
`src/scroll.py`. CustomTkinter's built-in wheel handling does not cover the
`<TouchpadScroll>` events that Tcl/Tk 9 sends on macOS, which made the window
unscrollable on a laptop — see that module's docstring.
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox as msgbox
from typing import Optional

import customtkinter as ctk
from openai import OpenAI

from . import __version__
from .config import (
    AppConfig,
    DEFAULT_CN_COLOR,
    DEFAULT_CN_FONT,
    DEFAULT_EN_COLOR,
    DEFAULT_EN_FONT,
    DEFAULT_LANGUAGE_OPTIONS,
    NAMING_PRESETS,
    DEFAULT_SYSTEM_PROMPT,
)
from .processor import Processor
from .translator import normalize_base_url, friendly_error
from .ass_generator import resolve_output_name
from .font_picker import ask_font
from .fonts import is_installed
from .scroll import install_scroll_support

# --------------------------------------------------------------------- #
# Look & feel
# --------------------------------------------------------------------- #
ACCENT = "#2a9d8f"
ACCENT_HOVER = "#248577"
DANGER = "#c1121f"
DANGER_HOVER = "#9d0f19"
# Fill used for a button that is currently inactive, so a disabled action never
# looks clickable (CustomTkinter only greys the label, not the background).
IDLE_FILL = ("gray82", "gray27")
IDLE_FILL_HOVER = ("gray76", "gray33")

# Fonts are instantiated in App.__init__ (a CTkFont needs a Tk root window to
# exist, which is only true after super().__init__()).

# Log line colours — mid-brightness tones readable on both light and dark themes.
LOG_COLORS = {
    "head": (ACCENT, ACCENT),
    "ok": ("#0d7a4f", "#3ecf8e"),
    "warn": ("#b4690e", "#f0a83c"),
    "error": ("#c1121f", "#ff6b6b"),
}

# Provider presets: (base_url, model_hint). An empty model hint keeps whatever
# the user already typed (important for local servers with a loaded .gguf).
PROVIDER_PRESETS = {
    "llama.cpp（本地）": ("http://127.0.0.1:8765/v1", ""),
    "LM Studio（本地）": ("http://127.0.0.1:1234/v1", ""),
    "Ollama（本地）": ("http://localhost:11434/v1", "qwen2.5"),
    "vLLM（本地）": ("http://localhost:8000/v1", ""),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "自定义": ("", ""),
}

LABEL_W = 180  # width of the label column inside every section card

# Fixed "video frame" background for the subtitle colour preview. Subtitles are
# always drawn over video, so a dark box previews them realistically and stays
# independent of the app's light/dark theme.
PREVIEW_BG = "#141414"


def _readable_text_color(hex_color: str) -> str:
    """Black or white text, whichever contrasts better with `hex_color`."""
    s = hex_color.lstrip("#")
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#FFFFFF"
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if luminance > 0.55 else "#FFFFFF"


def _normalize_hex(value: Optional[str], default: str) -> str:
    """Return a clean '#RRGGBB' string, or `default` if `value` is unusable."""
    s = (value or "").strip().lstrip("#").upper()
    if len(s) == 3:  # shorthand #ABC -> #AABBCC
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6 and all(ch in "0123456789ABCDEF" for ch in s):
        return f"#{s}"
    return default


def _shorten(path: str, limit: int = 56) -> str:
    """Shorten a long path in the middle so it stays readable in a label."""
    if len(path) <= limit:
        return path
    half = limit // 2
    return f"{path[:half]}…{path[-half:]}"


def _log_tag(msg: str) -> Optional[str]:
    """Pick a log colour tag for a message, or None for the default style."""
    m = msg.strip()
    if m.startswith("==="):
        return "head"
    if any(k in m for k in ("致命错误", "FATAL", "ERROR", "连接失败",
                            "Skipped due to error", "错误")):
        return "error"
    if "⚠️" in m or "注意" in m:
        return "warn"
    if any(k in m for k in ("-> Saved", "连接成功", "Finished", "完成",
                            "已自动补翻", "Found ")):
        return "ok"
    return None


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Subtitle Translator — Extract · Translate · ASS")

        # The config is read before the window is sized, so the last window
        # geometry can be restored instead of always opening at the default.
        self.cfg = AppConfig()
        self._fit_to_screen(980, 1020)

        # Fonts (must be created after the Tk root window exists).
        self.F_TITLE = ctk.CTkFont(size=21, weight="bold")
        self.F_SUBTITLE = ctk.CTkFont(size=12)
        self.F_SECTION = ctk.CTkFont(size=14, weight="bold")
        self.F_BODY = ctk.CTkFont(size=12)
        self.F_BODY_BOLD = ctk.CTkFont(size=12, weight="bold")
        self.F_MONO = ctk.CTkFont(family="Menlo", size=11)
        self.F_MONO_BOLD = ctk.CTkFont(family="Menlo", size=11, weight="bold")
        self.F_HINT = ctk.CTkFont(size=11)
        self.F_BIG = ctk.CTkFont(size=15, weight="bold")
        # The subtitle preview fonts are built on demand from the user's font
        # choice (see _set_font), so they are not defined here.

        self.processing = False
        self.cancel_event: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None

        self._build_ui()
        self._load_config_to_ui()
        self._update_naming_preview()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===================================================================== #
    # UI construction
    # ===================================================================== #
    def _fit_to_screen(self, width: int, height: int):
        """Size and centre the window so it fits *this* Mac's screen.

        The layout is designed for 980x1020, which is taller than the usable
        area of a 13-inch MacBook — without this clamp the Run and Progress
        cards would sit below the screen edge. Everything lives in a scrollable
        frame, so a smaller window is harmless.

        A geometry saved from the previous session wins, as long as it still
        fits the current screen (a window dragged to a disconnected display
        must not reopen off-screen).
        """
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            self.geometry(f"{width}x{height}")
            return

        min_w, min_h = min(840, sw - 40), min(560, sh - 120)
        if self._restore_geometry(sw, sh, min_w, min_h):
            self.minsize(min_w, min_h)
            return

        w = min(width, max(600, sw - 40))     # leave room for window edges
        h = min(height, max(480, sh - 120))   # menu bar + Dock + title bar
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min_w, min_h)

    def _restore_geometry(self, sw: int, sh: int, min_w: int, min_h: int) -> bool:
        """Apply the saved 'WxH+X+Y' geometry; False if unusable or off-screen."""
        saved = str(self.cfg.get("window_geometry", "") or "")
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", saved.strip())
        if not match:
            return False
        w, h, x, y = (int(g) for g in match.groups())
        # Ignore a window that would land (mostly) outside the visible screen.
        if x < -50 or y < -50 or x > sw - 120 or y > sh - 80:
            return False
        w = max(min_w, min(w, sw))
        h = max(min_h, min(h, sh - 40))
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        return True

    def _bind_shortcuts(self):
        """The handful of menu-less shortcuts a Mac app is expected to have."""
        self.bind_all("<Command-o>", lambda _e: self._pick_folder("video"))
        self.bind_all("<Command-Shift-O>", lambda _e: self._pick_folder("output"))
        self.bind_all("<Command-r>", lambda _e: self._on_start())

    def _build_ui(self):
        # Restore the saved theme before any widget is created, so the very
        # first paint already uses the right palette (no flash of light/dark).
        mode = str(self.cfg.get("appearance_mode", "System") or "System")
        self.appearance_mode = mode if mode in ("System", "Dark", "Light") else "System"
        ctk.set_appearance_mode(self.appearance_mode)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_statusbar()

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=("gray78", "gray32"),
            scrollbar_button_hover_color=("gray60", "gray42"),
        )
        self.scroll.grid(row=1, column=0, padx=20, pady=14, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)
        self._sec_row = 0
        # Wheel + trackpad + keyboard scrolling (see src/scroll.py).
        install_scroll_support(self.scroll)

        self._build_folders()
        self._build_subtitle_opts()
        self._build_llm_opts()
        self._build_options()
        self._build_run()
        self._build_progress()
        self._set_controls_enabled(True)   # idle: Start active, Cancel greyed

    # ---- header -------------------------------------------------------- #
    def _build_header(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=72)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        bar.pack_propagate(False)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=20)
        ctk.CTkLabel(left, text="🎬  Subtitle Translator", font=self.F_TITLE,
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(left, text="Extract  ·  Translate  ·  Bilingual ASS",
                     font=self.F_SUBTITLE, text_color=("gray35", "gray65"),
                     anchor="w").grid(row=1, column=0, sticky="w")

        self.theme_var = ctk.StringVar(value=self.appearance_mode)
        ctk.CTkSegmentedButton(
            bar, values=["System", "Dark", "Light"], variable=self.theme_var,
            width=200, command=self._on_theme_change,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        ).grid(row=0, column=1, sticky="e", padx=20)

    def _on_theme_change(self, mode: str):
        ctk.set_appearance_mode(mode)
        self.appearance_mode = mode
        self.theme_var.set(mode)     # keep the switcher in sync when set in code
        # Remember the choice: the next launch should look the same.
        self.cfg.set("appearance_mode", mode)
        self.cfg.save()
        # Log tag colours are plain tkinter colours, so re-apply per theme.
        self._apply_log_colors()

    # ---- status bar ---------------------------------------------------- #
    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=28)
        bar.grid(row=2, column=0, sticky="ew")
        self.status_label = ctk.CTkLabel(
            bar, text="就绪 · 依赖 ffmpeg · 配置已保存到 ~/.subtitle_translator.json",
            font=self.F_HINT, text_color=("gray35", "gray65"), anchor="w",
        )
        self.status_label.pack(side="left", padx=20, fill="x", expand=True)
        ctk.CTkLabel(
            bar, text=f"v{__version__}", font=self.F_HINT,
            text_color=("gray45", "gray55"), anchor="e",
        ).pack(side="right", padx=(0, 20))

    # ---- reusable section card ----------------------------------------- #
    def _section(self, title: str, hint: str = ""):
        """Create a titled card and return its *inner content* frame.

        The title lives in its own row, so content rows can never overlap it.
        """
        card = ctk.CTkFrame(self.scroll, corner_radius=12, border_width=1,
                            border_color=("gray86", "gray22"))
        card.grid(row=self._sec_row, column=0, sticky="ew", padx=0, pady=(0, 14))
        self._sec_row += 1
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=title, font=self.F_SECTION, text_color=ACCENT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        if hint:
            ctk.CTkLabel(head, text=hint, font=self.F_HINT,
                         text_color=("gray45", "gray60"), anchor="w",
                         ).grid(row=0, column=1, sticky="e")

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(10, 16))
        body.grid_columnconfigure(0, weight=0, minsize=LABEL_W)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=0)
        return body

    def _row_label(self, parent, text: str, row: int):
        ctk.CTkLabel(parent, text=text, font=self.F_BODY, anchor="w").grid(
            row=row, column=0, sticky="w", pady=7)

    # ---- Folders ------------------------------------------------------- #
    def _build_folders(self):
        f = self._section("Folders", "选择包含视频的文件夹")

        for row, (icon, text, which) in enumerate(
            [("📁", "Select Video Folder", "video"),
             ("📂", "Select Output Folder", "output")]
        ):
            ctk.CTkButton(
                f, text=f"{icon}  {text}", width=180, height=34, font=self.F_BODY,
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                command=lambda w=which: self._pick_folder(w),
            ).grid(row=row, column=0, sticky="w", pady=6)

            box = ctk.CTkFrame(f, corner_radius=8, border_width=1,
                               fg_color=("gray96", "gray20"))
            box.grid(row=row, column=1, columnspan=2, sticky="ew",
                     padx=(12, 0), pady=6)
            lbl = ctk.CTkLabel(box, text="（未选择）", font=self.F_BODY, anchor="w")
            lbl.pack(fill="x", padx=10, pady=6)
            setattr(self, f"{which}_path_label", lbl)

    # ---- Subtitle options ---------------------------------------------- #
    def _build_subtitle_opts(self):
        f = self._section("Subtitle Options", "源语言 · 命名 · 颜色 · 字体")

        self._row_label(f, "Source language", 0)
        self.lang_var = ctk.StringVar(value="English")
        self.lang_menu = ctk.CTkComboBox(
            f, values=[n for n, _ in DEFAULT_LANGUAGE_OPTIONS],
            variable=self.lang_var, width=240, state="readonly",
            command=lambda _v: self._update_naming_preview(),
        )
        self.lang_menu.grid(row=0, column=1, sticky="w", pady=7)

        self._row_label(f, "Output naming", 1)
        self.naming_var = ctk.StringVar(value=NAMING_PRESETS[0])
        self.naming_menu = ctk.CTkComboBox(
            f, values=NAMING_PRESETS + ["Custom template…"],
            variable=self.naming_var, width=240,
            command=self._on_naming_change,
        )
        self.naming_menu.grid(row=1, column=1, sticky="w", pady=7)

        self.custom_naming = ctk.CTkEntry(
            f, placeholder_text="{original_name}.{source_lang}.zh.ass", height=32)
        self.custom_naming.grid(row=2, column=1, columnspan=2, sticky="ew",
                                pady=(2, 0))
        self.custom_naming.grid_remove()

        self.naming_preview = ctk.CTkLabel(
            f, text="", font=self.F_HINT, text_color=("gray45", "gray60"), anchor="w")
        self.naming_preview.grid(row=3, column=1, columnspan=2, sticky="w",
                                 pady=(4, 0))

        # ---- Subtitle text colours ------------------------------------- #
        self._row_label(f, "Chinese color", 4)
        self.cn_color_btn = ctk.CTkButton(
            f, text=DEFAULT_CN_COLOR, width=150, height=32,
            font=self.F_BODY_BOLD, command=lambda: self._pick_color("cn"))
        self.cn_color_btn.grid(row=4, column=1, sticky="w", pady=7)

        self._row_label(f, "English color", 5)
        self.en_color_btn = ctk.CTkButton(
            f, text=DEFAULT_EN_COLOR, width=150, height=32,
            font=self.F_BODY_BOLD, command=lambda: self._pick_color("en"))
        self.en_color_btn.grid(row=5, column=1, sticky="w", pady=7)

        ctk.CTkButton(
            f, text="↺  重置颜色", width=120, height=28, font=self.F_HINT,
            fg_color="transparent", hover_color=("gray88", "gray25"),
            text_color=("gray35", "gray70"), border_width=1,
            command=self._reset_colors,
        ).grid(row=5, column=2, sticky="w", padx=(12, 0), pady=7)

        # ---- Subtitle fonts -------------------------------------------- #
        # The buttons label themselves in the chosen typeface, so the row is
        # its own preview. Clicking one opens the full font picker.
        self._fonts = {"cn": DEFAULT_CN_FONT, "en": DEFAULT_EN_FONT}

        self._row_label(f, "Chinese font", 6)
        self.cn_font_btn = self._font_button(f, "cn", DEFAULT_CN_FONT, row=6)

        self._row_label(f, "English font", 7)
        self.en_font_btn = self._font_button(f, "en", DEFAULT_EN_FONT, row=7)

        ctk.CTkButton(
            f, text="↺  重置字体", width=120, height=28, font=self.F_HINT,
            fg_color="transparent", hover_color=("gray88", "gray25"),
            text_color=("gray35", "gray70"), border_width=1,
            command=self._reset_fonts,
        ).grid(row=7, column=2, sticky="w", padx=(12, 0), pady=7)

        # Live preview: a dark "video frame" with both lines in their colours
        # *and* their fonts.
        preview = ctk.CTkFrame(f, fg_color=PREVIEW_BG, corner_radius=8, height=66)
        preview.grid(row=8, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        preview.grid_propagate(False)
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)
        self.preview_cn = ctk.CTkLabel(
            preview, text="我们需要谈谈。",
            font=ctk.CTkFont(family=DEFAULT_CN_FONT, size=15),
            text_color=DEFAULT_CN_COLOR, anchor="w")
        self.preview_cn.grid(row=0, column=0, sticky="w", padx=14)
        self.preview_en = ctk.CTkLabel(
            preview, text="We need to talk.",
            font=ctk.CTkFont(family=DEFAULT_EN_FONT, size=11),
            text_color=DEFAULT_EN_COLOR, anchor="w")
        self.preview_en.grid(row=1, column=0, sticky="w", padx=14, pady=(2, 0))

    def _font_button(self, parent, which: str, family: str, row: int):
        """A button that shows the current family *in* that family."""
        btn = ctk.CTkButton(
            parent, text=family, width=200, height=32, anchor="w",
            font=ctk.CTkFont(family=family, size=12),
            fg_color=("gray85", "gray28"), hover_color=("gray78", "gray35"),
            text_color=("gray15", "gray90"), border_width=1,
            border_color=("gray72", "gray45"),
            command=lambda: self._pick_font(which),
        )
        btn.grid(row=row, column=1, sticky="w", pady=7)
        return btn

    # ---- LLM configuration --------------------------------------------- #
    def _build_llm_opts(self):
        f = self._section("LLM Configuration", "OpenAI 兼容接口")

        self._row_label(f, "Provider preset", 0)
        self.provider_var = ctk.StringVar(value="自定义")
        ctk.CTkComboBox(
            f, values=list(PROVIDER_PRESETS), variable=self.provider_var,
            width=240, state="readonly", command=self._on_provider_change,
        ).grid(row=0, column=1, sticky="w", pady=7)

        self._row_label(f, "Base URL", 1)
        self.base_url_var = ctk.StringVar()
        ctk.CTkEntry(f, textvariable=self.base_url_var, height=32,
                     placeholder_text="https://api.openai.com/v1").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=7)

        self._row_label(f, "API Key", 2)
        self.api_key_var = ctk.StringVar()
        self.api_key_entry = ctk.CTkEntry(
            f, textvariable=self.api_key_var, height=32, show="*",
            placeholder_text="本地服务可填任意非空字符串")
        self.api_key_entry.grid(row=2, column=1, sticky="ew", pady=7)
        self.key_visible = False
        ctk.CTkButton(
            f, text="👁", width=38, height=32, font=self.F_BODY,
            fg_color=("gray85", "gray28"), hover_color=("gray78", "gray35"),
            text_color=("gray15", "gray90"), command=self._toggle_key_visibility,
        ).grid(row=2, column=2, sticky="w", padx=(8, 0), pady=7)

        self._row_label(f, "Model", 3)
        self.model_var = ctk.StringVar()
        ctk.CTkEntry(f, textvariable=self.model_var, height=32,
                     placeholder_text="gpt-4o-mini").grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=7)

        # Connectivity probe (own row frame so the status label keeps its width)
        probe = ctk.CTkFrame(f, fg_color="transparent")
        probe.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        self.test_btn = ctk.CTkButton(
            probe, text="🔌  测试连接 (Test)", width=180, height=34, font=self.F_BODY,
            fg_color=("gray85", "gray28"), hover_color=("gray78", "gray35"),
            text_color=("gray15", "gray90"), command=self._on_test_connection,
        )
        self.test_btn.pack(side="left")
        self.test_status = ctk.CTkLabel(probe, text="", font=self.F_BODY, anchor="w")
        self.test_status.pack(side="left", padx=(14, 0))

        self._row_label(f, "System Prompt", 5)
        self.sys_prompt = ctk.CTkTextbox(f, height=120, wrap="word", font=self.F_MONO)
        self.sys_prompt.grid(row=5, column=1, columnspan=2, sticky="ew", pady=7)
        ctk.CTkButton(
            f, text="↺  重置为默认", width=140, height=28, font=self.F_HINT,
            fg_color="transparent", hover_color=("gray88", "gray25"),
            text_color=("gray35", "gray70"), border_width=1,
            command=self._reset_system_prompt,
        ).grid(row=6, column=1, sticky="w", pady=(0, 4))

    # ---- Options ------------------------------------------------------- #
    def _build_options(self):
        f = self._section("Options", "批处理行为")

        self.overwrite_var = ctk.BooleanVar(value=False)
        self.keep_var = ctk.BooleanVar(value=False)
        self.skip_var = ctk.BooleanVar(value=True)
        self.cn_first_var = ctk.BooleanVar(value=True)

        items = [
            ("♻️  Overwrite existing files", self.overwrite_var, 0, 0),
            ("🗂️  Keep intermediate SRT files", self.keep_var, 0, 1),
            ("🇨🇳  Skip videos that already have Chinese subtitles", self.skip_var, 1, 0),
            ("⬆️  Chinese on top (primary line)", self.cn_first_var, 1, 1),
        ]
        for text, var, row, col in items:
            ctk.CTkCheckBox(f, text=text, variable=var, font=self.F_BODY,
                            checkbox_width=20, checkbox_height=20,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER).grid(
                row=row, column=1 + col, sticky="w", pady=8, padx=(0, 16))

    # ---- Run ----------------------------------------------------------- #
    def _build_run(self):
        f = self._section("Run", "开始 / 取消批处理")
        f.grid_columnconfigure(2, weight=0, minsize=170)
        self._row_label(f, "Action", 0)
        self.start_btn = ctk.CTkButton(
            f, text="▶   Start Processing", command=self._on_start,
            height=48, font=self.F_BIG, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            corner_radius=10,
        )
        self.start_btn.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=6)
        self.cancel_btn = ctk.CTkButton(
            f, text="■   Cancel", command=self._on_cancel,
            height=48, font=self.F_BIG, state="disabled", fg_color=DANGER,
            hover_color=DANGER_HOVER, corner_radius=10,
        )
        self.cancel_btn.grid(row=0, column=2, sticky="ew", padx=(8, 0), pady=6)

        # Quick access to the two folders, so a finished batch can be inspected
        # without going back to Finder.
        self._row_label(f, "Reveal", 1)
        reveal = ctk.CTkFrame(f, fg_color="transparent")
        reveal.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
        for text, which in (("📁  视频文件夹", "video"),
                            ("📂  输出文件夹", "output")):
            ctk.CTkButton(
                reveal, text=text, width=150, height=28, font=self.F_HINT,
                fg_color="transparent", hover_color=("gray88", "gray25"),
                text_color=("gray35", "gray70"), border_width=1,
                command=lambda w=which: self._reveal_folder(w),
            ).pack(side="left", padx=(0, 10))

    # ---- Progress + log ------------------------------------------------ #
    def _build_progress(self):
        f = self._section("Progress", "实时进度与日志")

        top = ctk.CTkFrame(f, fg_color="transparent")
        top.grid(row=0, column=1, columnspan=2, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.progress_detail = ctk.CTkLabel(top, text="Idle", font=self.F_BODY,
                                            anchor="w")
        self.progress_detail.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.progress_pct = ctk.CTkLabel(top, text="0%", font=self.F_BODY_BOLD,
                                         width=52, anchor="e")
        self.progress_pct.grid(row=0, column=1, sticky="e", pady=(0, 6))

        self.progress_bar = ctk.CTkProgressBar(
            f, height=14, corner_radius=7, progress_color=ACCENT)
        self.progress_bar.grid(row=1, column=1, columnspan=2, sticky="ew",
                               pady=(0, 12))
        self.progress_bar.set(0)

        log_head = ctk.CTkFrame(f, fg_color="transparent")
        log_head.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(0, 6))
        log_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_head, text="Log", font=self.F_BODY_BOLD,
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            log_head, text="🧹  Clear", width=100, height=26, font=self.F_HINT,
            fg_color="transparent", hover_color=("gray88", "gray25"),
            text_color=("gray35", "gray70"), border_width=1,
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(f, height=280, font=self.F_MONO, wrap="word")
        self.log_box.grid(row=3, column=1, columnspan=2, sticky="nsew")
        self.log_box.configure(state="disabled")
        self._apply_log_colors()

    def _apply_log_colors(self):
        """Re-apply log tag colours for the active theme.

        CTkTextbox.tag_config forwards to tkinter, which only accepts a single
        colour (no theme tuples), so we pick per appearance mode ourselves.
        """
        dark = ctk.get_appearance_mode().lower() == "dark"
        for tag, (light, dark_color) in LOG_COLORS.items():
            self.log_box.tag_config(tag, foreground=dark_color if dark else light)

    # ===================================================================== #
    # Config <-> UI
    # ===================================================================== #
    def _pick_folder(self, which: str):
        d = filedialog.askdirectory(
            title="Select Video Folder" if which == "video"
            else "Select Output Folder")
        if not d:
            return
        self.cfg.set(f"{which}_folder", d)
        self.cfg.save()          # remember the choice even if nothing is run
        getattr(self, f"{which}_path_label").configure(text=_shorten(d))
        self._update_naming_preview()

    def _reveal_folder(self, which: str):
        """Open the selected folder in Finder."""
        path = self.cfg.get(f"{which}_folder") or ""
        if not path or not Path(path).is_dir():
            msgbox.showinfo("尚未选择",
                            "请先选择文件夹。" if which == "video"
                            else "请先选择输出文件夹。")
            return
        try:
            subprocess.run(["open", path], check=False)
        except Exception as exc:
            self._append_log(f"⚠️ 无法打开文件夹：{exc}")

    def _on_close(self):
        """Persist the window geometry, then quit (asking if work is running)."""
        if self.processing and not msgbox.askyesno(
                "正在处理", "批处理仍在进行中，退出会中断当前任务。确定要退出吗？"):
            return
        try:
            self.cfg.set("window_geometry", self.winfo_geometry())
            self.cfg.save()
        except Exception:
            pass
        self.destroy()

    def _on_provider_change(self, choice: str):
        base, model = PROVIDER_PRESETS.get(choice, ("", ""))
        if base:
            self.base_url_var.set(base)
        if model:  # never wipe a model the user typed for a local server
            self.model_var.set(model)
        self._set_status(f"已填入预设：{choice}")

    def _toggle_key_visibility(self):
        self.key_visible = not self.key_visible
        self.api_key_entry.configure(show="" if self.key_visible else "*")

    def _reset_system_prompt(self):
        self.sys_prompt.delete("0.0", "end")
        self.sys_prompt.insert("0.0", DEFAULT_SYSTEM_PROMPT)
        self._set_status("System Prompt 已重置为默认")

    # ---- subtitle colours ---------------------------------------------- #
    def _color_hex(self, which: str) -> str:
        """Current hex for 'cn' or 'en' (always a clean '#RRGGBB')."""
        btn = self.cn_color_btn if which == "cn" else self.en_color_btn
        return _normalize_hex(btn.cget("text"), DEFAULT_CN_COLOR)

    def _set_color(self, which: str, hex_color: str):
        """Apply a colour to its swatch button and the preview line."""
        hex_color = _normalize_hex(hex_color, DEFAULT_CN_COLOR)
        btn = self.cn_color_btn if which == "cn" else self.en_color_btn
        btn.configure(
            text=hex_color, fg_color=hex_color,
            hover_color=hex_color, border_width=1,
            text_color=_readable_text_color(hex_color),
        )
        preview = self.preview_cn if which == "cn" else self.preview_en
        preview.configure(text_color=hex_color)

    def _pick_color(self, which: str):
        """Open the native colour panel for the Chinese or English line."""
        label = "中文" if which == "cn" else "英文"
        result = colorchooser.askcolor(
            color=self._color_hex(which), title=f"选择{label}字幕颜色")
        if not result or not result[1]:
            return  # user cancelled
        self._set_color(which, result[1])
        self._set_status(f"{label}字幕颜色：{self._color_hex(which)}")

    def _reset_colors(self):
        self._set_color("cn", DEFAULT_CN_COLOR)
        self._set_color("en", DEFAULT_EN_COLOR)
        self._set_status("字幕颜色已重置为默认（白 / 浅灰）")

    # ---- subtitle fonts ------------------------------------------------ #
    def _default_font(self, which: str) -> str:
        return DEFAULT_CN_FONT if which == "cn" else DEFAULT_EN_FONT

    def _set_font(self, which: str, family: str):
        """Apply a family to its button (labelled in that family) + preview."""
        family = (family or "").strip() or self._default_font(which)
        self._fonts[which] = family

        btn = self.cn_font_btn if which == "cn" else self.en_font_btn
        btn.configure(text=family, font=ctk.CTkFont(family=family, size=12))

        preview = self.preview_cn if which == "cn" else self.preview_en
        size = 15 if which == "cn" else 11
        preview.configure(font=ctk.CTkFont(family=family, size=size))

    def _pick_font(self, which: str):
        """Open the font picker for the Chinese or English subtitle line."""
        label = "中文字幕" if which == "cn" else "英文原文字幕"
        chosen = ask_font(
            self, title=f"选择{label}字体",
            current=self._fonts[which],
            # The Chinese line needs CJK glyphs; the original line usually
            # doesn't, so don't hide everything else behind that filter.
            prefer_cjk=(which == "cn"),
        )
        if not chosen:
            return  # user cancelled
        self._set_font(which, chosen)
        self._set_status(f"{label}字体：{chosen}")

    def _reset_fonts(self):
        self._set_font("cn", DEFAULT_CN_FONT)
        self._set_font("en", DEFAULT_EN_FONT)
        self._set_status(f"字幕字体已重置为默认（{DEFAULT_CN_FONT} / {DEFAULT_EN_FONT}）")

    def _warn_missing_fonts(self):
        """Log a note when a saved font is not installed on this machine."""
        missing = sorted({f for f in self._fonts.values() if not is_installed(f)})
        if missing:
            self._append_log(
                f"⚠️ 注意：字体「{'、'.join(missing)}」在本机未安装，"
                "预览和生成的字幕会回退到系统默认字体。"
            )

    def _on_naming_change(self, value):
        if value.startswith("Custom"):
            self.custom_naming.grid()
        else:
            self.custom_naming.grid_remove()
        self._update_naming_preview()

    def _update_naming_preview(self):
        """Show what the chosen naming template would produce."""
        template = self._current_naming_template()
        code = next((c for n, c in DEFAULT_LANGUAGE_OPTIONS
                     if n == self.lang_var.get()), "en")
        try:
            sample = resolve_output_name(template, "Show - S01E01", code)
            self.naming_preview.configure(text=f"示例：{sample}")
        except Exception:
            self.naming_preview.configure(text="")

    def _current_naming_template(self) -> str:
        if self.naming_var.get().startswith("Custom"):
            return self.custom_naming.get().strip() or NAMING_PRESETS[0]
        return self.naming_var.get()

    def _load_config_to_ui(self):
        c = self.cfg.data
        for which in ("video", "output"):
            p = c.get(f"{which}_folder") or ""
            getattr(self, f"{which}_path_label").configure(
                text=_shorten(p) if p else "（未选择）")

        code = c.get("source_language", "en")
        self.lang_var.set(next(
            (n for n, cd in DEFAULT_LANGUAGE_OPTIONS if cd == code), "English"))

        nf = c.get("naming_format", NAMING_PRESETS[0])
        if nf in NAMING_PRESETS:
            self.naming_var.set(nf)
        else:
            self.naming_var.set("Custom template…")
            self.custom_naming.grid()
            self.custom_naming.insert(0, nf)

        base = normalize_base_url(c.get("base_url", ""))
        self.base_url_var.set(base)
        self.api_key_var.set(c.get("api_key", ""))
        self.model_var.set(c.get("model_name", ""))
        # Preselect the matching provider preset when we recognise the URL.
        for name, (pbase, _pmodel) in PROVIDER_PRESETS.items():
            if pbase and pbase == base:
                self.provider_var.set(name)
                break
        else:
            self.provider_var.set("自定义")

        self.sys_prompt.insert("0.0", c.get("system_prompt", DEFAULT_SYSTEM_PROMPT))
        self.overwrite_var.set(c.get("overwrite_existing", False))
        self.keep_var.set(c.get("keep_intermediate", False))
        self.skip_var.set(c.get("skip_existing_chinese", True))
        self.cn_first_var.set(c.get("cn_first", True))
        self._set_color("cn", c.get("cn_color", DEFAULT_CN_COLOR))
        self._set_color("en", c.get("en_color", DEFAULT_EN_COLOR))
        self._set_font("cn", c.get("cn_font", DEFAULT_CN_FONT))
        self._set_font("en", c.get("en_font", DEFAULT_EN_FONT))
        self._warn_missing_fonts()

    def _collect_config(self):
        c = self.cfg.data
        name = self.lang_var.get()
        c["source_language"] = next(
            (cd for n, cd in DEFAULT_LANGUAGE_OPTIONS if n == name), "en")
        c["naming_format"] = self._current_naming_template()
        c["base_url"] = normalize_base_url(self.base_url_var.get().strip())
        c["api_key"] = self.api_key_var.get().strip()
        c["model_name"] = self.model_var.get().strip()
        c["system_prompt"] = (
            self.sys_prompt.get("0.0", "end").strip() or DEFAULT_SYSTEM_PROMPT)
        c["overwrite_existing"] = self.overwrite_var.get()
        c["keep_intermediate"] = self.keep_var.get()
        c["skip_existing_chinese"] = self.skip_var.get()
        c["cn_first"] = self.cn_first_var.get()
        c["cn_color"] = self._color_hex("cn")
        c["en_color"] = self._color_hex("en")
        c["cn_font"] = self._fonts["cn"]
        c["en_font"] = self._fonts["en"]
        self.cfg.save()

    # ===================================================================== #
    # Run / worker
    # ===================================================================== #
    def _on_start(self):
        if self.processing:
            return
        self._collect_config()
        c = self.cfg.data

        if not c["video_folder"] or not Path(c["video_folder"]).is_dir():
            msgbox.showerror("缺少输入", "请先选择有效的视频文件夹。")
            return
        if not c["output_folder"] or not Path(c["output_folder"]).is_dir():
            msgbox.showerror("缺少输出", "请先选择有效的输出文件夹。")
            return
        if not c["base_url"]:
            msgbox.showerror("缺少 Base URL", "请填写 LLM 的 Base URL。")
            return
        if not c["api_key"]:
            msgbox.showerror("缺少 API Key",
                             "请填写 API Key（本地服务可填任意非空字符串）。")
            return

        self.processing = True
        self.cancel_event = threading.Event()
        self._set_controls_enabled(False)
        self.progress_bar.set(0)
        self._update_progress(0, "正在启动…")
        self._set_status("正在处理…")
        self._append_log("=== 开始批处理 ===")

        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _run_worker(self):
        c = self.cfg.data
        lang_label = next(
            (n for n, cd in DEFAULT_LANGUAGE_OPTIONS if cd == c["source_language"]),
            "English",
        )
        processor = Processor(
            video_folder=Path(c["video_folder"]),
            output_folder=Path(c["output_folder"]),
            source_lang=c["source_language"],
            source_lang_label=lang_label,
            naming_format=c["naming_format"],
            base_url=c["base_url"],
            api_key=c["api_key"],
            model=c["model_name"],
            system_prompt=c["system_prompt"],
            overwrite=c["overwrite_existing"],
            keep_intermediate=c["keep_intermediate"],
            skip_existing_chinese=c["skip_existing_chinese"],
            cn_first=c["cn_first"],
            batch_size=c.get("batch_size", 40),
            max_line_retries=c.get("max_line_retries", 2),
            cn_color=c["cn_color"],
            en_color=c["en_color"],
            cn_font=c["cn_font"],
            en_font=c["en_font"],
        )
        try:
            summary = processor.run(
                log_cb=self._safe_log,
                progress_cb=self._safe_progress,
                cancel_event=self.cancel_event,
            )
            self._safe_log("=== 完成 ===")
            self._safe_log(summary)
            self._safe_status(summary)
        except InterruptedError:
            self._safe_log("=== 已被用户取消 ===")
            self._safe_status("已取消")
        except Exception as exc:
            self._safe_log(f"致命错误：{exc}")
            self._safe_status("出错，详见日志")
        finally:
            self.processing = False
            self._safe_enable_controls(True)

    def _on_cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self._append_log("正在取消…（会在当前批次结束后停止）")
            self._set_status("正在取消…")

    # ===================================================================== #
    # LLM connectivity test (runs off the UI thread)
    # ===================================================================== #
    def _on_test_connection(self):
        base = normalize_base_url(self.base_url_var.get().strip())
        key = self.api_key_var.get().strip() or "sk-no-key-required"
        model = self.model_var.get().strip()
        if not base:
            self._append_log("[Test] 请先填写 Base URL。")
            return
        self._append_log(f"[Test] 正在连接 {base} …")
        self.test_btn.configure(state="disabled")
        self.test_status.configure(text="连接中…",
                                   text_color=("gray45", "gray60"))
        threading.Thread(
            target=self._run_test_connection,
            args=(base, key, model), daemon=True,
        ).start()

    def _run_test_connection(self, base, key, model):
        ok = False
        try:
            client = OpenAI(base_url=base, api_key=key)
            names = [m.id for m in getattr(client.models.list(), "data", [])]
            shown = ", ".join(names[:8]) or "（服务未返回模型列表）"
            self._safe_log(f"[Test] 连接成功（{base}）。可用模型：{shown}")
            if model and names and model not in names:
                self._safe_log(
                    f"[Test] 注意：Model「{model}」不在服务端列表中，"
                    f"实际可用的是：{names[0]}")
            ok = True
        except Exception as exc:
            self._safe_log(f"[Test] 连接失败：{friendly_error(exc)}")
        finally:
            self.after(0, lambda: self._finish_test(ok))

    def _finish_test(self, ok: bool):
        self.test_btn.configure(state="normal")
        self.test_status.configure(
            text="连接成功 ✓" if ok else "连接失败 ✗",
            text_color=("#0d7a4f", "#3ecf8e") if ok else ("#c1121f", "#ff6b6b"),
        )

    # ===================================================================== #
    # Thread-safe UI updates
    # ===================================================================== #
    def _safe_log(self, msg):
        self.after(0, lambda: self._append_log(msg))

    def _safe_progress(self, frac, detail):
        self.after(0, lambda: self._update_progress(frac, detail))

    def _safe_status(self, text):
        self.after(0, lambda: self._set_status(text))

    def _safe_enable_controls(self, enabled):
        self.after(0, lambda: self._set_controls_enabled(enabled))

    def _append_log(self, msg):
        """Append one line, keeping the view pinned only if it already was.

        Auto-scrolling unconditionally would yank the user back to the bottom
        while they are reading an earlier part of the log.
        """
        pinned = self._log_pinned_to_bottom()
        self.log_box.configure(state="normal")
        tag = _log_tag(msg)
        self.log_box.insert("end", msg + "\n", (tag,) if tag else ())
        self.log_box.configure(state="disabled")
        if pinned:
            self.log_box.see("end")

    def _log_pinned_to_bottom(self) -> bool:
        """True when the log view is (practically) scrolled to its end."""
        try:
            return self.log_box._textbox.yview()[1] > 0.999
        except Exception:
            return True  # unknown state: behave like the old code

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("0.0", "end")
        self.log_box.configure(state="disabled")

    def _update_progress(self, frac, detail):
        frac = max(0.0, min(1.0, frac))
        self.progress_bar.set(frac)
        self.progress_pct.configure(text=f"{frac * 100:.0f}%")
        self.progress_detail.configure(text=detail)

    def _set_status(self, text):
        self.status_label.configure(text=text)

    def _set_controls_enabled(self, enabled):
        """Idle state: Start active, Cancel greyed out (and the reverse)."""
        self.start_btn.configure(
            state="normal" if enabled else "disabled",
            fg_color=ACCENT if enabled else IDLE_FILL,
            hover_color=ACCENT_HOVER if enabled else IDLE_FILL_HOVER,
        )
        self.cancel_btn.configure(
            state="disabled" if enabled else "normal",
            fg_color=IDLE_FILL if enabled else DANGER,
            hover_color=IDLE_FILL_HOVER if enabled else DANGER_HOVER,
        )
