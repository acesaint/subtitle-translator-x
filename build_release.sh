#!/bin/bash
# =====================================================================
#  build_release.sh — build a self-contained "Subtitle Translator.app"
#
#  This is the "like an app you downloaded" build: Python, Tcl/Tk and every
#  dependency are copied *inside* the bundle, so the Mac you copy it to
#  needs no Python, no virtual environment and no Terminal.
#
#      ./build_release.sh                 # build the .app (+ .dmg)
#      ./build_release.sh --no-dmg        # .app only
#      ./build_release.sh --bundle-ffmpeg /path/to/ffmpeg
#
#  --bundle-ffmpeg copies a **static** ffmpeg (and ffprobe from the same
#  folder) into the app, which then needs nothing installed at all. A
#  Homebrew ffmpeg is dynamically linked against /opt/homebrew/lib and will
#  NOT work on another Mac, so only pass a static build.
#
#  Distribution note: the result is ad-hoc signed, which is fine for your own
#  Macs. To hand it to other people without a Gatekeeper warning you need an
#  Apple Developer ID (paid) plus notarisation — see README.
# =====================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Subtitle Translator"
BUNDLE_ID="com.workbuddy.subtitle-translator"
DIST_DIR="$PROJECT_DIR/dist"
BUNDLE="$DIST_DIR/$APP_NAME.app"
BUILD_DIR="$PROJECT_DIR/build"

BUNDLE_FFMPEG=""
MAKE_DMG=1

while [ $# -gt 0 ]; do
    case "$1" in
        --no-dmg) MAKE_DMG=0 ;;
        --bundle-ffmpeg) BUNDLE_FFMPEG="${2:?--bundle-ffmpeg needs a path}"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }

# --- sanity ----------------------------------------------------------------
[ -x "$PROJECT_DIR/.venv/bin/python" ] || {
    echo "ERROR: .venv not found. Run ./setup.sh first." >&2
    exit 1
}

if ! "$PROJECT_DIR/.venv/bin/python" -c "import PyInstaller" >/dev/null 2>&1; then
    say "==> Installing PyInstaller (one-off, ~10 MB)"
    "$PROJECT_DIR/.venv/bin/python" -m pip install --quiet pyinstaller || {
        echo "ERROR: could not install PyInstaller (no network?)." >&2
        exit 1
    }
fi

PYI_VERSION="$("$PROJECT_DIR/.venv/bin/python" -c 'import PyInstaller; print(PyInstaller.__version__)')"

# Version lives in src/__init__.py — one place to bump, used by the app's status
# bar and by Info.plist / the DMG filename.
APP_VERSION="$("$PROJECT_DIR/.venv/bin/python" -c \
    'import sys; sys.path.insert(0, "."); from src import __version__; print(__version__)' \
    2>/dev/null || echo "0.0.0")"
APP_BUILD="${APP_BUILD:-$APP_VERSION}"

say "==> Building $APP_NAME.app with PyInstaller $PYI_VERSION"
say "    version $APP_VERSION ($APP_BUILD)"
say "    python: $("$PROJECT_DIR/.venv/bin/python" -c 'import sys,tkinter; print("Python %s / Tk %s" % (sys.version.split()[0], tkinter.TkVersion))')"

# --- icon ------------------------------------------------------------------
ICON="$BUILD_DIR/icon.icns"
mkdir -p "$BUILD_DIR"
if [ -x "$PROJECT_DIR/tools/make_icon.sh" ] && "$PROJECT_DIR/tools/make_icon.sh" "$ICON"; then
    say "    icon:   $ICON"
else
    ICON=""
    say "    icon:   skipped (make_icon.sh failed; generic icon will be used)"
fi

# --- optional static ffmpeg ------------------------------------------------
EXTRA_ARGS=()
if [ -n "$BUNDLE_FFMPEG" ]; then
    [ -x "$BUNDLE_FFMPEG" ] || { echo "ERROR: not executable: $BUNDLE_FFMPEG" >&2; exit 1; }

    # A static binary only depends on system libraries; anything else means
    # the copy would break on a machine without Homebrew.
    if otool -L "$BUNDLE_FFMPEG" | tail -n +2 | grep -qvE '/usr/lib/|/System/Library/'; then
        echo "ERROR: $BUNDLE_FFMPEG is dynamically linked (Homebrew build)." >&2
        echo "       Bundling it would produce an app that only runs on this Mac." >&2
        echo "       Use a static build instead (e.g. from osxexperts.net / evermeet.cx)." >&2
        exit 1
    fi

    EXTRA_ARGS+=(--add-binary "$BUNDLE_FFMPEG:bin")
    say "    ffmpeg: bundling $BUNDLE_FFMPEG"

    FFPROBE="$(dirname "$BUNDLE_FFMPEG")/ffprobe"
    if [ -x "$FFPROBE" ]; then
        EXTRA_ARGS+=(--add-binary "$FFPROBE:bin")
    else
        echo "WARNING: no ffprobe next to $BUNDLE_FFMPEG — subtitle probing will fail." >&2
    fi
else
    say "    ffmpeg: not bundled — the app will use ffmpeg from PATH"
    say "            (pass --bundle-ffmpeg /path/to/static/ffmpeg for a fully"
    say "             self-contained app)"
fi

# --- freeze ----------------------------------------------------------------
# PyInstaller writes a onedir build (a folder *and* a .app around it). Build it
# into build/dist so dist/ only ever holds the finished .app — no 40 MB
# duplicate to clean up afterwards.
STAGE_DIST="$BUILD_DIR/dist"
rm -rf "$BUILD_DIR/$APP_NAME" "$STAGE_DIST" "$BUNDLE"

PYI_ARGS=(
    --noconfirm
    --clean
    --windowed
    --name "$APP_NAME"
    --osx-bundle-identifier "$BUNDLE_ID"
    --distpath "$STAGE_DIST"
    --workpath "$BUILD_DIR/pyinstaller"
    --specpath "$BUILD_DIR"
    --collect-data customtkinter
    --exclude-module tkinter.test
    --exclude-module pytest
)
[ -n "$ICON" ] && PYI_ARGS+=(--icon "$ICON")
[ ${#EXTRA_ARGS[@]} -gt 0 ] && PYI_ARGS+=("${EXTRA_ARGS[@]}")

"$PROJECT_DIR/.venv/bin/pyinstaller" "${PYI_ARGS[@]}" main.py

[ -d "$STAGE_DIST/$APP_NAME.app" ] || { echo "ERROR: PyInstaller did not produce $APP_NAME.app" >&2; exit 1; }
mkdir -p "$DIST_DIR"
mv "$STAGE_DIST/$APP_NAME.app" "$BUNDLE"
[ -d "$BUNDLE" ] || { echo "ERROR: could not move the app into $DIST_DIR" >&2; exit 1; }

# --- Info.plist polish -----------------------------------------------------
# PyInstaller writes a minimal plist (version 0.0.0, no minimum OS, no
# category). Finder, Gatekeeper and "Get Info" all read these fields, so set
# them explicitly — it is what makes the bundle look like a real app.
PLIST="$BUNDLE/Contents/Info.plist"
plist_set() {   # plist_set <key> <value>  — value is always written as a string
    /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PLIST" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST" 2>/dev/null \
        || true
}
plist_set CFBundleShortVersionString "$APP_VERSION"
plist_set CFBundleVersion            "$APP_BUILD"
plist_set LSMinimumSystemVersion     "11.0"
plist_set LSApplicationCategoryType  "public.app-category.video"
say "==> Info.plist: version $APP_VERSION ($APP_BUILD), macOS 11+, category video"

# --- sign (ad-hoc) ---------------------------------------------------------
# An ad-hoc signature keeps macOS happy on the machine that built it and on
# your own Macs. Distributing to strangers additionally needs a Developer ID
# certificate + notarisation.
say "==> Signing (ad-hoc)"
# codesign refuses a bundle whose files carry extended attributes ("resource
# fork, Finder information, or similar detritus not allowed"). A project folder
# synced by iCloud Drive / Dropbox puts com.apple.FinderInfo on the *bundle
# root*, which -r alone does not always clear, so wipe the root explicitly too.
xattr -cr "$BUNDLE" 2>/dev/null || true
xattr -c "$BUNDLE" 2>/dev/null || true
codesign --force --deep --sign - "$BUNDLE" 2>&1 | sed 's/^/    /' || true
# Plain --verify (not --strict): a project folder synced by iCloud Drive puts
# com.apple.FinderInfo back on the bundle root immediately after it is cleared,
# and --strict reports that as "detritus". It does not affect the signature
# itself, which is what Gatekeeper checks.
if codesign --verify --deep "$BUNDLE" 2>/dev/null; then
    say "    signature OK"
else
    say "    WARNING: signature could not be verified (the app still runs locally)"
fi

# --- dmg -------------------------------------------------------------------
DMG=""
if [ "$MAKE_DMG" = "1" ]; then
    STAGE="$BUILD_DIR/dmg"
    rm -rf "$STAGE"; mkdir -p "$STAGE"
    cp -R "$BUNDLE" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    # Version in the filename, no spaces: nicer in a browser download and in a
    # GitHub release asset URL.
    DMG="$DIST_DIR/Subtitle-Translator-$APP_VERSION.dmg"
    rm -f "$DMG" "$DIST_DIR/$APP_NAME.dmg"
    say "==> Creating $(basename "$DMG")"
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
    rm -rf "$STAGE"
fi

# --- report ----------------------------------------------------------------
say ""
say "==> Done"
say "    app:  $(du -sh "$BUNDLE" | cut -f1)  $BUNDLE"
ARCHS="$(lipo -archs "$BUNDLE/Contents/MacOS/$APP_NAME" 2>/dev/null || echo unknown)"
say "    arch: $ARCHS"
case "$ARCHS" in
    *arm64*x86_64*|*x86_64*arm64*) : ;;
    *) say "    note: this build runs on $ARCHS Macs only. For an Intel Mac,"
       say "          build on/with a universal2 Python (python.org installer)." ;;
esac
[ "$MAKE_DMG" = "1" ] && say "    dmg: $(du -sh "$DMG" | cut -f1)  $DMG"
say ""
say "    Copy the .app (or .dmg) to another Mac and double-click — no Python,"
say "    no setup.sh needed there."
say ""
say "    First launch after copying over AirDrop/USB/download:"
say "      xattr -dr com.apple.quarantine \"$APP_NAME.app\""
say "    (or right-click > Open). A Developer ID + notarisation removes that step."
