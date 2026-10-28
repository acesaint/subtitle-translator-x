#!/bin/bash
# =====================================================================
#  tools/make_icon.sh <output.icns>
#
#  Draws the app icon (teal tile, white 字, subtitle bar) and packs it as
#  .icns. Shared by build_app.sh (thin bundle) and build_release.sh
#  (self-contained bundle).
#
#  The drawing itself is done by make_icon.js through the Cocoa bridge that
#  ships with macOS — no ffmpeg, no Pillow, no image libraries to install.
#  `sips` and `iconutil` are part of the base system.
#
#  Exits non-zero when the icon cannot be produced — callers treat that as
#  "carry on without a custom icon", never as a build failure.
# =====================================================================
set -euo pipefail

OUT="${1:?usage: make_icon.sh <output.icns>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v osascript >/dev/null 2>&1 || exit 1
command -v sips      >/dev/null 2>&1 || exit 1
command -v iconutil  >/dev/null 2>&1 || exit 1
[ -f "$HERE/make_icon.js" ] || exit 1

TMP="$(mktemp -d -t st_icon)"
trap 'rm -rf "$TMP"' EXIT

# --- render the master image -----------------------------------------------
RAW="$TMP/raw.png"
osascript -l JavaScript "$HERE/make_icon.js" "$RAW" >/dev/null 2>&1 || exit 1
[ -s "$RAW" ] || exit 1

# The Cocoa drawing happens in points; on a Retina screen the backing store
# is 2x. Normalise to exactly 1024x1024 before slicing.
MASTER="$TMP/master.png"
sips -z 1024 1024 "$RAW" --out "$MASTER" >/dev/null 2>&1 || exit 1

# --- slice into an .iconset -------------------------------------------------
ICONSET="$TMP/icon.iconset"
mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
    sips -z "$sz" "$sz" "$MASTER" \
        --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1 || exit 1
    sips -z $((sz * 2)) $((sz * 2)) "$MASTER" \
        --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1 || exit 1
done

# --- pack -------------------------------------------------------------------
mkdir -p "$(dirname "$OUT")"
iconutil -c icns "$ICONSET" -o "$OUT" >/dev/null 2>&1 || exit 1
[ -s "$OUT" ]
