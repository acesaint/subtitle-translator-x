"""Entry point for the Subtitle Translator desktop application.

IMPORTANT (macOS): Python's bundled Tcl/Tk must be >= 8.6. The Tk 8.5 that
ships with the system Python (e.g. /usr/bin/python3 on macOS) cannot render
windows on modern macOS — the window appears completely blank. We detect that
here and fail with a clear message instead of showing an empty window.

The app runs two ways:

* from source — ``.venv/bin/python main.py`` (see setup.sh);
* frozen — the ``Subtitle Translator.app`` produced by ``build_release.sh``,
  where Python, Tcl/Tk and the dependencies live inside the bundle and no
  Python installation is needed on the machine.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import tkinter

MIN_TK_VERSION = 8.6

# Where a frozen (Finder-launched) app writes its diagnostics.
LOG_PATH = Path.home() / "Library" / "Logs" / "SubtitleTranslator.log"


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def _ensure_tcl_library_path() -> None:
    """Point Tcl/Tk at their script libraries when the default lookup fails.

    Launching from Finder (double-clicking a .app bundle) gives the process a
    minimal environment. On some Python builds Tcl then cannot locate its own
    ``init.tcl`` and dies with "Cannot find a usable init.tcl". The scripts
    live next to the *base* interpreter (``<base>/lib/tcl9.0``), so we derive
    them from ``sys.base_prefix`` and export the standard variables before any
    Tcl interpreter is created. Already-set variables are left untouched.
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return

    # Inside a PyInstaller bundle the Tcl/Tk script libraries ship with the app
    # and the bootloader already exports TCL_LIBRARY / TK_LIBRARY.
    if _is_frozen():
        return

    base = getattr(sys, "base_prefix", sys.prefix)
    libdir = os.path.join(base, "lib")
    if not os.path.isdir(libdir):
        return

    for env_name, marker, pattern in (
        ("TCL_LIBRARY", "init.tcl", "tcl*"),
        ("TK_LIBRARY", "tk.tcl", "tk*"),
    ):
        if os.environ.get(env_name):
            continue
        for cand in sorted(glob.glob(os.path.join(libdir, pattern)), reverse=True):
            if os.path.isfile(os.path.join(cand, marker)):
                os.environ[env_name] = cand
                break


def _ensure_bundled_tools() -> None:
    """Make sure `ffmpeg` / `ffprobe` can be found once the app is running.

    Two cases matter:

    * a frozen app may carry its own copies under ``Contents/Frameworks/bin``
      (see build_release.sh) — those win over anything installed on the Mac;
    * launched from Finder, the process inherits a minimal PATH
      (``/usr/bin:/bin:/usr/sbin:/sbin``) with no Homebrew, so a perfectly
      good ``/opt/homebrew/bin/ffmpeg`` would look missing.
    """
    base = getattr(sys, "_MEIPASS", "") if _is_frozen() else ""
    candidates = []
    if base:
        candidates += [os.path.join(base, "bin"), base]
    candidates += ["/opt/homebrew/bin", "/usr/local/bin"]

    existing = os.environ.get("PATH", "").split(os.pathsep)
    prepend = [p for p in candidates if os.path.isdir(p) and p not in existing]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + existing)


def _redirect_output_to_log() -> None:
    """Send stdout/stderr to a log file when launched from Finder.

    A double-clicked bundle has no terminal, so anything Python prints — a
    traceback included — is lost. Keeping a log makes "the app does not start"
    diagnosable (``open ~/Library/Logs/SubtitleTranslator.log``).
    """
    if not _is_frozen():
        return  # running from a terminal: leave the streams alone
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    except Exception:
        return
    sys.stdout = handle
    sys.stderr = handle


def _report_startup_problem(title: str, message: str) -> None:
    """Print to stderr and, if possible, show a native dialog."""
    print("=" * 72, file=sys.stderr)
    print(f"ERROR: {title}", file=sys.stderr)
    print(message, file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    if shutil.which("osascript"):
        short = message.replace('"', "'").replace("\n", " ")
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display alert "Subtitle Translator cannot start" '
                 f'message "{short}" as critical'],
                check=False, timeout=60,
            )
        except Exception:
            pass


def _check_tk_version() -> bool:
    """Return True if the running Tcl/Tk can render windows correctly."""
    if tkinter.TkVersion >= MIN_TK_VERSION:
        return True

    if _is_frozen():
        # A bundle ships its own Tk; if it is too old the bundle is broken.
        _report_startup_problem(
            "incompatible Tcl/Tk version",
            f"The bundled Tcl/Tk is {tkinter.TkVersion}, which cannot draw "
            "windows on this macOS. Rebuild the app on a Mac with a newer "
            "Python (Tk 8.6+).",
        )
        return False

    detail = (
        f"当前 Python 使用的 Tcl/Tk 版本为 {tkinter.TkVersion}，"
        "在较新的 macOS 上无法正常绘制窗口（窗口会是一片空白）。\n\n"
        "请使用自带 Tk 8.6+ 的 Python，例如 Homebrew 安装的 Python：\n"
        "  brew install python@3.12 python-tk@3.12\n"
        "然后运行 ./setup.sh 重新创建虚拟环境。\n\n"
        "（详见 README 的 Troubleshooting 一节。）"
    )
    _report_startup_problem(
        "incompatible Tcl/Tk version",
        f"Tcl/Tk {tkinter.TkVersion} is too old and cannot draw windows on "
        "this macOS. Run ./setup.sh with a Python that has Tk 8.6+ "
        "(e.g. Homebrew python@3.12). See README > Troubleshooting.",
    )
    print(detail, file=sys.stderr)
    return False


def _check_ffmpeg() -> None:
    """Warn once at startup when ffmpeg is missing.

    Extraction is the first pipeline stage, so without ffmpeg nothing works.
    A frozen app may be installed on a Mac that has never seen Homebrew; the
    per-file error would only appear after the user picks a folder and hits
    Start, so say it up front instead.
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    _report_startup_problem(
        "ffmpeg is not installed",
        "Subtitle extraction needs ffmpeg. Install it from the Terminal with "
        "`brew install ffmpeg` (https://brew.sh), then reopen the app. "
        "Everything else — translation and ASS output — is built in.",
    )


def main() -> int:
    _redirect_output_to_log()
    _ensure_tcl_library_path()
    _ensure_bundled_tools()

    if not _check_tk_version():
        return 1

    _check_ffmpeg()

    # Imported late so a broken/missing environment produces a readable
    # message instead of a traceback that Finder never shows.
    try:
        import customtkinter as ctk
        from src.gui import App
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        if _is_frozen():
            _report_startup_problem(
                "the app bundle is incomplete",
                f"A required module is missing from the bundle ({missing}). "
                "Rebuild it with ./build_release.sh.",
            )
        else:
            _report_startup_problem(
                "Python dependencies are missing",
                f"Missing module: {missing}. Open Terminal in the project "
                "folder and run ./setup.sh to create the environment.",
            )
        return 1

    # System / Dark / Light is also selectable from the in-app header menu.
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    try:
        app = App()
        app.mainloop()
    except Exception:
        # Never die silently in a Finder-launched bundle: log the traceback and
        # tell the user where to find it.
        traceback.print_exc()
        _report_startup_problem(
            "unexpected error",
            "The window closed because of an unexpected error. Details were "
            f"written to {LOG_PATH}.",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
