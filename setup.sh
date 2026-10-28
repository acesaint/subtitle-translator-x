#!/bin/bash
# =====================================================================
#  setup.sh — prepare this copy of Subtitle Translator for *this* Mac
#
#  Run this once after copying the project folder to another computer:
#
#      ./setup.sh
#
#  It picks a Python whose Tcl/Tk can actually draw windows, (re)creates
#  .venv, installs the dependencies, checks ffmpeg, and rebuilds the .app.
#  Running it again later is safe and fast.
# =====================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

MIN_PY="3.10"
MIN_TK="8.6"

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

say "==> Subtitle Translator setup"
say "    project: $PROJECT_DIR"

# --------------------------------------------------------------------- #
# 1. Find a Python that can actually draw windows
# --------------------------------------------------------------------- #
# macOS's built-in /usr/bin/python3 is 3.9 with Tk 8.5: importing works, but
# any window it creates is blank. So a candidate is only accepted when BOTH
# the interpreter version and its Tk version are new enough.
usable() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys, tkinter
assert sys.version_info >= (3, 10), sys.version
assert tkinter.TkVersion >= 8.6, tkinter.TkVersion
PY
}

describe() {
    "$1" -c 'import sys, tkinter
print("Python %s / Tk %s" % (sys.version.split()[0], tkinter.TkVersion))' 2>/dev/null \
        || echo "not usable"
}

CANDIDATES=()
[ -n "${PYTHON:-}" ] && CANDIDATES+=("$PYTHON")   # explicit override wins
CANDIDATES+=(
    python3.13 python3.12 python3.11 python3.10 python3
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10
    /usr/local/bin/python3.13 /usr/local/bin/python3.12
    /usr/local/bin/python3.11 /usr/local/bin/python3.10
)
# python.org installers keep every version side by side.
for p in /Library/Frameworks/Python.framework/Versions/*/bin/python3; do
    [ -x "$p" ] && CANDIDATES+=("$p")
done

PYTHON_BIN=""
SKIPPED=""
for cand in "${CANDIDATES[@]}"; do
    [ -n "$cand" ] || continue
    cmd="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$cmd" ] && [ -x "$cmd" ] || continue
    if usable "$cmd"; then
        PYTHON_BIN="$cmd"
        break
    fi
    SKIPPED="$SKIPPED
      - $cmd ($(describe "$cmd"))"
done

if [ -z "$PYTHON_BIN" ]; then
    cat >&2 <<EOF

ERROR: no suitable Python found.

Subtitle Translator needs Python $MIN_PY+ whose Tcl/Tk is at least $MIN_TK.
The macOS system Python (/usr/bin/python3) is 3.9 with Tk 8.5 — it cannot draw
windows on modern macOS, so the app would open a completely blank window.

Install one with Homebrew:

    brew install python@3.12 python-tk@3.12

then run this script again. If you already have a suitable Python somewhere
else, point the script at it:

    PYTHON=/path/to/python3 ./setup.sh
EOF
    [ -n "$SKIPPED" ] && printf '\nChecked and skipped:%s\n' "$SKIPPED" >&2
    exit 1
fi

say "    python: $PYTHON_BIN ($(describe "$PYTHON_BIN"))"

# --------------------------------------------------------------------- #
# 2. (Re)create the virtual environment
# --------------------------------------------------------------------- #
# A .venv copied from another Mac still points at that machine's interpreter.
# It is reusable only if its python runs here AND is the same major.minor as
# the interpreter we just picked; otherwise it is rebuilt from scratch.
VENV="$PROJECT_DIR/.venv"
need_venv=1
venv_note=""

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sys" >/dev/null 2>&1; then
    venv_ver="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
    want_ver="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if [ "$venv_ver" = "$want_ver" ]; then
        need_venv=0
        venv_note="reusing existing .venv (Python $venv_ver)"
    else
        venv_note="rebuilding .venv (was Python ${venv_ver:-unknown}, want $want_ver)"
    fi
else
    venv_note="creating .venv"
fi

say "==> $venv_note"
if [ "$need_venv" = "1" ]; then
    rm -rf "$VENV"
    "$PYTHON_BIN" -m venv "$VENV" || {
        echo "ERROR: could not create the virtual environment." >&2
        exit 1
    }
fi

# --------------------------------------------------------------------- #
# 3. Dependencies
# --------------------------------------------------------------------- #
say "==> Installing dependencies from requirements.txt"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV/bin/python" -m pip install --quiet -r requirements.txt; then
    cat >&2 <<EOF

ERROR: pip could not install the dependencies.

Check your internet connection and try again. Behind a proxy, set:
    export HTTPS_PROXY=http://host:port
EOF
    exit 1
fi

# Prove the environment really works before building the app around it.
if ! "$VENV/bin/python" -c "import customtkinter, openai" >/dev/null 2>&1; then
    echo "ERROR: the new environment cannot import customtkinter / openai." >&2
    exit 1
fi

# --------------------------------------------------------------------- #
# 4. ffmpeg (needed to extract subtitles, installed by Homebrew)
# --------------------------------------------------------------------- #
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    say "    ffmpeg: $(command -v ffmpeg)"
else
    warn "ffmpeg / ffprobe not found — subtitles cannot be extracted."
    warn "Install it with:  brew install ffmpeg"
fi

# --------------------------------------------------------------------- #
# 5. Rebuild the double-clickable app
# --------------------------------------------------------------------- #
if [ -x "$PROJECT_DIR/build_app.sh" ]; then
    say "==> Rebuilding the app bundle"
    "$PROJECT_DIR/build_app.sh"
else
    warn "build_app.sh not found or not executable; skipping the .app bundle."
    chmod +x "$PROJECT_DIR/build_app.sh" 2>/dev/null || true
fi

say ""
say "Done. Double-click \"Subtitle Translator.app\" in this folder,"
say "or run:  .venv/bin/python main.py"
say "Log file: ~/Library/Logs/SubtitleTranslator.log"
