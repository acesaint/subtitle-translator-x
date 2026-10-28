"""A modal font picker that previews the installed typefaces.

CustomTkinter's combobox can only draw all of its entries in one font, which
makes choosing a *font* from a dropdown a guessing game. This dialog shows a
list where every row renders itself: the family name and a sample line are
drawn in the family they describe, so the choice is visual.

The list is **pooled**: a fixed set of row widgets is created once and then
re-bound to whichever families match the current search. Two reasons:

* Speed — filtering re-labels a few dozen rows instead of rebuilding hundreds.
* Stability — macOS/Tk becomes unstable when a modal dialog owns hundreds of
  live widgets and is then torn down; a fixed page keeps the count constant.

Usage::

    from .font_picker import ask_font
    chosen = ask_font(self, title="选择中文字体", current="PingFang SC")
    if chosen: ...
"""
from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from .fonts import is_cjk_font, list_font_families
from .scroll import install_scroll_support

ACCENT = "#2a9d8f"
ACCENT_HOVER = "#248577"
ROW_SELECTED = ("#d7ece8", "#1e3b37")
ROW_HOVER = ("gray90", "gray25")

# How many rows exist at any time. Comfortably larger than the "Chinese fonts
# only" result set (~65 on a stock macOS) so that view is complete, small enough
# to keep the dialog instant and stable — a modal owning hundreds of widgets
# makes Tk crash on teardown. Larger result sets are narrowed with the search box.
PAGE_SIZE = 72

# Drawn in every row with that row's own family: Latin, digits and Chinese, so
# a font that cannot render CJK shows its gaps immediately.
ROW_SAMPLE = "我们需要谈谈  We need to talk.  123"

# The big preview at the bottom of the dialog.
PREVIEW_BG = "#141414"
PREVIEW_TEXT = "我们需要谈谈。"


class FontPickerDialog(ctk.CTkToplevel):
    """Modal list of installed fonts with live per-row previews."""

    def __init__(self, parent, *, title: str = "选择字体",
                 current: str = "", prefer_cjk: bool = True):
        super().__init__(parent)

        self.result: Optional[str] = None
        self._current = (current or "").strip()
        self._prefer_cjk = prefer_cjk
        self._selected = self._current
        self._matches: list[str] = []
        self._row_family: list[str] = []
        self._font_cache: dict[tuple[str, int], ctk.CTkFont] = {}
        self._search_job: Optional[str] = None
        self._closed = False

        self.title(title)
        width, height = self._center_on_parent(parent, 780, 660)
        self.minsize(min(620, width), min(480, height))
        self.transient(parent)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        families = list_font_families()
        # When picking a Chinese font, put CJK-capable families on top — they
        # are what the user wants 95% of the time.
        self._order = sorted(
            families,
            key=(lambda f: (not is_cjk_font(f), f.lower())) if prefer_cjk
            else str.lower,
        )

        self._build_header(title)
        self._build_tools()
        self._build_list()
        self._build_footer()

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())

        self._apply_filter(force=True)
        self._select(self._current)
        self._scroll_to_selected()
        # Give the window manager a moment before grabbing focus, otherwise
        # macOS occasionally refuses the grab ("window not viewable").
        self.after(150, self._activate)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _center_on_parent(self, parent, width: int, height: int):
        """Place the dialog over its parent, clamped to this Mac's screen.

        Returns the size actually used, so the caller can set a matching
        minimum size (a 13-inch laptop cannot show a 660 px tall dialog plus
        its chrome).
        """
        try:
            parent.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            width = min(width, max(560, sw - 80))
            height = min(height, max(420, sh - 120))

            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 3)
            x = max(0, min(x, sw - width))
            y = max(0, min(y, sh - height - 20))
        except Exception:
            x = y = 120
        self.geometry(f"{width}x{height}+{x}+{y}")
        return width, height

    def _build_header(self, title: str) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=title, font=ctk.CTkFont(size=17, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            head, text=f"{len(self._order)} 个字体已安装 · 双击直接使用",
            font=ctk.CTkFont(size=11), text_color=("gray45", "gray60"),
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

    def _build_tools(self) -> None:
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.grid(row=1, column=0, sticky="ew", padx=18, pady=(10, 8))
        tools.grid_columnconfigure(0, weight=1)

        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            tools, textvariable=self.search_var, height=34,
            placeholder_text="搜索字体名称，例如 PingFang、宋体、Noto…")
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_var.trace_add("write", lambda *_a: self._on_search())

        self.cjk_var = ctk.BooleanVar(value=self._prefer_cjk)
        ctk.CTkCheckBox(
            tools, text="仅中文字体", variable=self.cjk_var, width=110,
            font=ctk.CTkFont(size=12), checkbox_width=18, checkbox_height=18,
            command=lambda: self._apply_filter(force=True),
        ).grid(row=0, column=1, sticky="w", padx=(14, 0))

    def _build_list(self) -> None:
        self.list = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.list.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 10))
        self.list.grid_columnconfigure(0, weight=1)
        # Same wheel / trackpad fix as the main window (see src/scroll.py).
        install_scroll_support(self.list)

        # Fixed pool of rows, re-bound on every filter change.
        self._rows: list[tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkLabel]] = []
        for index in range(PAGE_SIZE):
            row = ctk.CTkFrame(self.list, height=32, corner_radius=6,
                               fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", padx=2, pady=1)
            row.grid_columnconfigure(0, weight=0, minsize=230)
            row.grid_columnconfigure(1, weight=1)

            name = ctk.CTkLabel(row, text="", anchor="w")
            name.grid(row=0, column=0, sticky="w", padx=(10, 12), pady=4)
            sample = ctk.CTkLabel(
                row, text=ROW_SAMPLE, anchor="e", text_color=("gray40", "gray62"))
            sample.grid(row=0, column=1, sticky="e", padx=(0, 10), pady=4)

            for widget in (row, name, sample):
                widget.bind("<Button-1>", lambda _e, i=index: self._click(i))
                widget.bind("<Double-Button-1>",
                            lambda _e, i=index: self._double_click(i))
                widget.bind("<Enter>", lambda _e, i=index: self._hover(i, True))
                widget.bind("<Leave>", lambda _e, i=index: self._hover(i, False))

            self._rows.append((row, name, sample))
            self._row_family.append("")

        self._empty = ctk.CTkLabel(
            self.list, text="没有匹配的字体", font=ctk.CTkFont(size=12),
            text_color=("gray45", "gray60"))

    def _build_footer(self) -> None:
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 16))
        foot.grid_columnconfigure(0, weight=1)

        box = ctk.CTkFrame(foot, fg_color=PREVIEW_BG, corner_radius=8, height=74)
        box.grid(row=0, column=0, columnspan=2, sticky="ew")
        box.grid_propagate(False)
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)

        self.preview = ctk.CTkLabel(
            box, text=PREVIEW_TEXT, font=ctk.CTkFont(size=22),
            text_color="#FFFFFF", anchor="w")
        self.preview.grid(row=0, column=0, sticky="w", padx=16)

        self.warning = ctk.CTkLabel(
            foot, text="", font=ctk.CTkFont(size=11),
            text_color=("#b4690e", "#f0a83c"), anchor="w")
        self.warning.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.count_label = ctk.CTkLabel(
            foot, text="", font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray60"), anchor="e")
        self.count_label.grid(row=1, column=1, sticky="e", pady=(6, 0))

        buttons = ctk.CTkFrame(foot, fg_color="transparent")
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

        ctk.CTkButton(
            buttons, text="取消", width=110, height=34,
            fg_color="transparent", hover_color=("gray88", "gray25"),
            text_color=("gray25", "gray85"), border_width=1,
            command=self._cancel,
        ).pack(side="left", padx=(0, 10))

        self.ok_btn = ctk.CTkButton(
            buttons, text="使用此字体", width=150, height=34,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._confirm)
        self.ok_btn.pack(side="left")

    # ------------------------------------------------------------------ #
    # Font objects
    # ------------------------------------------------------------------ #
    def _font(self, family: str, size: int) -> ctk.CTkFont:
        """Reuse one CTkFont per (family, size) instead of creating new ones."""
        key = (family, size)
        font = self._font_cache.get(key)
        if font is None:
            font = ctk.CTkFont(family=family, size=size)
            self._font_cache[key] = font
        return font

    # ------------------------------------------------------------------ #
    # Behaviour
    # ------------------------------------------------------------------ #
    def _activate(self) -> None:
        try:
            self.grab_set()
        except Exception:
            pass
        try:
            self.search_entry.focus_set()
        except Exception:
            pass

    def _on_search(self) -> None:
        """Debounce so a fast typist doesn't re-render on every keystroke."""
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.after(120, lambda: self._apply_filter())

    def _apply_filter(self, force: bool = False) -> None:
        self._search_job = None
        query = self.search_var.get().strip().lower()
        cjk_only = bool(self.cjk_var.get())

        matches = [
            f for f in self._order
            if (not query or query in f.lower())
            and (not cjk_only or is_cjk_font(f))
        ]
        if not force and matches == self._matches:
            return
        self._matches = matches

        page = matches[:PAGE_SIZE]
        for index, (row, name, sample) in enumerate(self._rows):
            if index < len(page):
                family = page[index]
                self._row_family[index] = family
                name.configure(text=family, font=self._font(family, 13))
                sample.configure(font=self._font(family, 12))
                row.configure(
                    fg_color=ROW_SELECTED if family == self._selected
                    else "transparent")
                row.grid()
            else:
                self._row_family[index] = ""
                row.grid_remove()

        if matches:
            self._empty.grid_remove()
        else:
            self._empty.grid(row=0, column=0, pady=20)

        hidden = len(matches) - len(page)
        self.count_label.configure(
            text=(f"共 {len(matches)} 个，显示前 {len(page)} 个 · "
                  "输入关键字缩小范围")
            if hidden > 0 else f"共 {len(matches)} 个")

        # A new filter starts at the top of the list.
        try:
            self.list._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _click(self, index: int) -> None:
        family = self._row_family[index] if index < len(self._row_family) else ""
        if family:
            self._select(family)

    def _double_click(self, index: int) -> None:
        family = self._row_family[index] if index < len(self._row_family) else ""
        if family:
            self._select(family)
            self._confirm()

    def _hover(self, index: int, entering: bool) -> None:
        if index >= len(self._rows):
            return
        family = self._row_family[index]
        if not family or family == self._selected:
            return
        self._rows[index][0].configure(
            fg_color=ROW_HOVER if entering else "transparent")

    def _select(self, family: str) -> None:
        """Remember `family`; highlight its row if that row is on screen."""
        family = (family or "").strip()
        self._selected = family

        for index, (row, _name, _sample) in enumerate(self._rows):
            if not self._row_family[index]:
                continue
            row.configure(
                fg_color=ROW_SELECTED if self._row_family[index] == family
                else "transparent")

        if family:
            self.preview.configure(text=PREVIEW_TEXT,
                                   font=self._font(family, 22))
            suffix = "" if is_cjk_font(family) else " — 该字体可能不含中文字形"
            self.warning.configure(text=f"已选择：{family}{suffix}")
            self.ok_btn.configure(state="normal")
        else:
            self.warning.configure(text="")
            self.ok_btn.configure(state="disabled")

    def _scroll_to_selected(self) -> None:
        """Bring the current font into view when the dialog opens."""
        try:
            if self._selected not in self._matches[:PAGE_SIZE]:
                return
            self.update_idletasks()
            canvas = self.list._parent_canvas
            total = max(1, self.list.winfo_height())
            index = self._matches.index(self._selected)
            canvas.yview_moveto(max(0.0, min(1.0, (index * 34 - 60) / total)))
        except Exception:
            pass  # purely cosmetic

    def _confirm(self) -> None:
        if not self._selected:
            return
        self.result = self._selected
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        if getattr(self, "_closed", False):
            return  # Return key + button click can both fire; close only once
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def ask_font(parent, *, title: str = "选择字体", current: str = "",
             prefer_cjk: bool = True) -> Optional[str]:
    """Open the picker modally; return the chosen family, or None if cancelled."""
    dialog = FontPickerDialog(parent, title=title, current=current,
                              prefer_cjk=prefer_cjk)
    try:
        parent.wait_window(dialog)
    except Exception:
        pass
    return dialog.result
