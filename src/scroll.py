"""Wheel / trackpad scrolling for ``CTkScrollableFrame``.

Why this module exists
======================
CustomTkinter listens for ``<MouseWheel>`` only.  Since **Tcl/Tk 9** — the Tk
that ships with current Python builds — macOS reports *precise* scrolling from
a trackpad (and from a Magic Mouse) as a **separate event**, ``<TouchpadScroll>``;
``<MouseWheel>`` is now produced only by a classic notched wheel.  A MacBook
therefore sent no scroll event that CustomTkinter understood, so the page could
not be scrolled with the device people actually use.

``<TouchpadScroll>`` is also unusual: its ``%D`` field carries **two** signed
16-bit deltas packed into one integer (x in the high word, y in the low word)
and the values are in *pixels*, not in "lines" — see ``tk::PreciseScrollDeltas``
in Tk's own ``tk.tcl``.

This module binds every event Tk really sends, decodes it, and scrolls the
canvas by pixels::

    <TouchpadScroll>   macOS trackpad / Magic Mouse        (Tk >= 9)
    <MouseWheel>       classic wheel, Windows, macOS Tk 8.6
    <Button-4/5>       X11 / Linux wheels
    <Prior>/<Next>/<Home>/<End>   keyboard, when no text field has focus

Behaviour that falls out of the design:

* **Pixel accurate** — the canvas is moved with ``yview_moveto``, so a
  trackpad's fine-grained deltas are honoured instead of being rounded to
  "lines".  No acceleration is invented; macOS already applies its own.
* **Clamped** — the page stops exactly at the top and at the bottom.
* **Chained** — a text box under the pointer (the log, the system prompt)
  scrolls itself first; the page only takes over once that box reached its end,
  which is what every native app does.
* **Scoped** — only the scroll frame that contains the pointer reacts, so a
  modal dialog (the font picker) and the page behind it never fight.

Usage::

    from .scroll import install_scroll_support

    page = ctk.CTkScrollableFrame(self)
    install_scroll_support(page)      # once per scroll frame, idempotent
"""
from __future__ import annotations

import tkinter
# Pixels moved per notch of a classic (non-precise) mouse wheel.  macOS itself
# scrolls ~3 lines per notch, and a line is ~16 px in this UI.
WHEEL_NOTCH_PX = 48

# Fraction of the viewport moved by Page Up / Page Down (native apps use ~90%).
PAGE_KEY_RATIO = 0.85

# Events CustomTkinter binds itself and we must take over.
_TAKEOVER_SEQUENCES = ("<MouseWheel>", "<TouchpadScroll>", "<Button-4>", "<Button-5>")

# Keyboard-driven scrolling. Tk delivers these to the focused widget, so they
# are matched by window instead of by widget ancestry.
_KEY_KINDS = ("page_up", "page_down", "home", "end")

# Every live scroller in this process.  Events are dispatched to the one whose
# scroll frame contains the widget under the pointer.
_SCROLLERS: list["SmoothScroller"] = []


# ---------------------------------------------------------------------- #
# Event decoding
# ---------------------------------------------------------------------- #
def decode_touchpad_delta(packed: int) -> tuple[int, int]:
    """Split Tk 9's packed ``<TouchpadScroll>`` delta into ``(dx, dy)``.

    Tk packs both axes into the single integer it passes as ``%D``: the
    horizontal delta in the high 16 bits, the vertical delta in the low 16 bits,
    both signed.  (This mirrors ``tk::PreciseScrollDeltas``.)

    >>> decode_touchpad_delta((-3) & 0xFFFF)
    (0, -3)
    """
    try:
        value = int(packed)
    except (TypeError, ValueError):
        return (0, 0)
    dx = value >> 16                      # arithmetic shift keeps the sign
    low = value & 0xFFFF
    dy = low - 0x10000 if low >= 0x8000 else low
    return (dx, dy)


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #
def install_scroll_support(frame) -> "SmoothScroller":
    """Make ``frame`` (a ``CTkScrollableFrame``) scroll with wheel *and* trackpad.

    Safe to call for every scroll frame in the app, any number of times: the
    global handlers are re-registered rather than duplicated, which also keeps
    them effective when a *new* ``CTkScrollableFrame`` (e.g. the font picker)
    re-adds CustomTkinter's own ``<MouseWheel>`` binding.
    """
    scroller = SmoothScroller(frame)
    _SCROLLERS.append(scroller)
    _install_global_handlers(frame)
    return scroller


class SmoothScroller:
    """Pixel-accurate wheel / trackpad / keyboard scrolling for one canvas."""

    def __init__(self, frame):
        self._frame = frame
        # CustomTkinter's public widgets hide the real tkinter.Canvas here; the
        # whole module is built on that one attribute.
        self._canvas = getattr(frame, "_parent_canvas", None)
        self._silence_scrollbar_wheel()

    # -- introspection -------------------------------------------------- #
    def alive(self) -> bool:
        """False once the underlying frame has been destroyed."""
        if self._canvas is None:
            return False
        try:
            return bool(self._frame.winfo_exists())
        except tkinter.TclError:
            return False

    def owns(self, widget) -> bool:
        """True when `widget` lives inside this scroller's scroll frame."""
        w = widget
        while w is not None:
            if w is self._frame:
                return True
            if isinstance(w, tkinter.Toplevel):
                return False  # reached another window (a dialog): not ours
            w = getattr(w, "master", None)
        return False

    def owns_window(self, widget) -> bool:
        """True when `widget` belongs to the same window as this scroller.

        Needed for keyboard events: Tk delivers those to the *focused* widget
        (often the root window itself), which is not necessarily a descendant
        of the scroll frame.
        """
        try:
            return (widget is not None
                    and widget.winfo_toplevel() is self._frame.winfo_toplevel())
        except (tkinter.TclError, AttributeError):
            return False

    # -- event entry point ---------------------------------------------- #
    def handle(self, event, kind: str) -> None:
        """React to one already-decoded input event.

        `kind` is one of ``wheel``, ``touchpad``, ``up``, ``down``,
        ``page_up``, ``page_down``, ``home``, ``end``.
        """
        if self._canvas is None:
            return

        if kind in _KEY_KINDS:
            if not self._keyboard_allowed(event):
                return
            if kind == "home":
                self.scroll_to_top()
            elif kind == "end":
                self.scroll_to_bottom()
            else:
                step = max(1, self._canvas.winfo_height()) * PAGE_KEY_RATIO
                self.scroll_by(-step if kind == "page_up" else step)
            return

        dy = self._pixels(event, kind)
        if not dy:
            return

        # Scroll chaining: a text box under the pointer gets the event first.
        inner = self._inner_text(event.widget)
        if inner is not None and self._text_can_scroll(inner, dy):
            return
        self.scroll_by(dy)

    # -- pixel conversions ---------------------------------------------- #
    def _pixels(self, event, kind: str) -> float:
        """Translate an event into pixels, positive meaning *scroll down*."""
        if kind == "touchpad":
            _dx, dy = decode_touchpad_delta(getattr(event, "delta", 0))
            # Precise deltas use the wheel convention: positive = content down
            # (i.e. scroll up), so the sign is flipped here.
            return -float(dy)

        if kind == "wheel":
            try:
                delta = int(getattr(event, "delta", 0))
            except (TypeError, ValueError):
                return 0.0
            if delta == 0:
                return 0.0
            # Windows reports multiples of 120 per notch; everywhere else the
            # value already counts notches (±1, ±2, …).
            notches = delta / 120.0 if abs(delta) >= 120 else float(delta)
            return -notches * WHEEL_NOTCH_PX

        if kind in ("up", "down"):
            return -WHEEL_NOTCH_PX if kind == "up" else WHEEL_NOTCH_PX

        return 0.0

    # -- movement ------------------------------------------------------- #
    def scroll_by(self, dy: float) -> None:
        """Move the page by `dy` pixels (positive scrolls towards the end)."""
        canvas = self._canvas
        if canvas is None:
            return
        try:
            region = canvas.bbox("all")
            view = canvas.winfo_height()
            first = canvas.yview()[0]
        except tkinter.TclError:
            return
        if not region or view <= 1:
            return

        total = region[3] - region[1]
        if total <= view:  # everything fits: nothing to scroll
            return

        current = first * total
        target = min(max(current + dy, 0.0), float(total - view))
        if abs(target - current) < 0.5:  # sub-pixel move or already at the edge
            return
        canvas.yview_moveto(target / total)

    def scroll_to_top(self) -> None:
        self._moveto(0.0)

    def scroll_to_bottom(self) -> None:
        self._moveto(1.0)

    def _moveto(self, fraction: float) -> None:
        if self._canvas is None:
            return
        try:
            self._canvas.yview_moveto(fraction)
        except tkinter.TclError:
            pass

    # -- helpers -------------------------------------------------------- #
    def _inner_text(self, widget):
        """The ``tkinter.Text`` under the pointer inside this frame, or None.

        ``CTkTextbox`` is a ``tkinter.Text`` plus decoration, and the event
        widget is the Text itself, so a plain isinstance walk is enough.
        """
        w = widget
        while w is not None and w is not self._frame:
            if isinstance(w, tkinter.Text):
                return w
            w = getattr(w, "master", None)
        return None

    @staticmethod
    def _text_can_scroll(text: tkinter.Text, dy: float) -> bool:
        """Can `text` still scroll further in the requested direction?

        Called *after* Tk's own class binding already scrolled the widget, so
        "at the end" here means "it could not move any further" — exactly the
        moment the page should take over.
        """
        try:
            first, last = text.yview()
        except tkinter.TclError:
            return False
        if last - first >= 1.0:  # whole content visible: no scrollbar needed
            return False
        return last < 1.0 if dy > 0 else first > 0.0

    def _keyboard_allowed(self, event) -> bool:
        """Keyboard scrolling must not steal keys from a focused text field.

        Page Up inside the log or the prompt box belongs to that box, not to
        the page.
        """
        try:
            focused = self._frame.focus_get()
        except (tkinter.TclError, KeyError):
            focused = None
        return not isinstance(focused, (tkinter.Text, tkinter.Entry,
                                        tkinter.Spinbox))

    def _silence_scrollbar_wheel(self) -> None:
        """Remove CustomTkinter's own wheel binding from the scrollbar.

        Otherwise a wheel event over the thin scrollbar would be applied twice
        (once by the scrollbar in "units", once by us in pixels).
        """
        bar_canvas = getattr(getattr(self._frame, "_scrollbar", None), "_canvas", None)
        if bar_canvas is None:
            return
        try:
            bar_canvas.unbind("<MouseWheel>")
        except tkinter.TclError:
            pass


# ---------------------------------------------------------------------- #
# Global dispatch
# ---------------------------------------------------------------------- #
def _install_global_handlers(widget) -> None:
    """(Re)register the process-wide wheel / trackpad / key handlers.

    Everything is bound on the ``all`` bind tag because the pointer can be over
    any of the dozens of nested widgets inside a scroll frame.  CustomTkinter
    does the same, so its handlers are removed first — otherwise both would run
    and the page would scroll twice.
    """
    try:
        root = widget._root()
    except Exception:
        return

    for sequence in _TAKEOVER_SEQUENCES:
        try:
            root.unbind_all(sequence)
        except tkinter.TclError:
            pass

    bindings = (
        ("<MouseWheel>", "wheel"),
        ("<TouchpadScroll>", "touchpad"),
        ("<Button-4>", "up"),
        ("<Button-5>", "down"),
        ("<Prior>", "page_up"),
        ("<Next>", "page_down"),
        ("<Home>", "home"),
        ("<End>", "end"),
    )
    for sequence, kind in bindings:
        try:
            root.bind_all(sequence, lambda e, k=kind: _dispatch(e, k), add="+")
        except tkinter.TclError:
            pass  # event not known to this Tk build (e.g. no TouchpadScroll)


def _dispatch(event, kind: str) -> None:
    """Route one event to the scroll frame that contains its widget."""
    # Drop scrollers whose frame has been destroyed (a closed dialog).
    for scroller in tuple(_SCROLLERS):
        if not scroller.alive():
            _SCROLLERS.remove(scroller)

    for scroller in _SCROLLERS:
        if scroller.owns(event.widget) or (
            kind in _KEY_KINDS and scroller.owns_window(event.widget)
        ):
            scroller.handle(event, kind)
            return
