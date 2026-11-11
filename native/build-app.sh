#!/bin/zsh
# Build "Burnboard.app": a universal binary (arm64 and x86_64), the app/ files inside, ad hoc signed.
#   native/build-app.sh               build and install to /Applications (the old app goes to the Trash)
#   native/build-app.sh --out DIR     build into DIR and install nothing
set -euo pipefail
cd "$(dirname "$0")/.."
INSTALL=1
if [ "${1:-}" = "--out" ]; then
  [ -n "${2:-}" ] || { echo "usage: $0 [--out DIR]" >&2; exit 2; }
  INSTALL=0; OUT_DIR="$2"
elif [ $# -gt 0 ]; then
  echo "usage: $0 [--out DIR]" >&2; exit 2
else
  OUT_DIR="$(mktemp -d)"
fi
mkdir -p "$OUT_DIR"
APP="$OUT_DIR/Burnboard.app"
[ ! -L "$APP" ] || { echo "$APP is a link, refusing" >&2; exit 1; }
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app/vendor"

MIN="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' native/Info.plist)"
WORK="$(mktemp -d)"
CHECK="$(mktemp -d)"
trap 'rm -rf "$WORK" "$CHECK"' EXIT
for arch in arm64 x86_64; do
  swiftc -O -target "$arch-apple-macos$MIN" -o "$WORK/Burnboard-$arch" native/main.swift -framework Cocoa -framework WebKit
done
lipo -create -output "$APP/Contents/MacOS/Burnboard" "$WORK/Burnboard-arm64" "$WORK/Burnboard-x86_64"

cp native/Info.plist "$APP/Contents/Info.plist"
cp icon/icon.icns "$APP/Contents/Resources/icon.icns"
# serve.py is only the development server and stays out of the app.
for f in app/*.py; do [ "${f:t}" = "serve.py" ] || cp "$f" "$APP/Contents/Resources/app/"; done
cp app/*.html "$APP/Contents/Resources/app/"
cp app/prices.json "$APP/Contents/Resources/app/"   # the shipped price list: used when a refresh cannot download one
cp app/vendor/d3.min.js "$APP/Contents/Resources/app/vendor/"

# Fail the build when a file the app needs at run time is missing from the bundle. cli.py is run the way the app
# runs it (-I -B, so nothing is written into the bundle) with throwaway data and cache folders and no network;
# `page` needs core.py, local-shell.html and page.html, and the price list must load from the bundle's own copy.
RES="$APP/Contents/Resources/app"
export USAGE_CLAUDE_OFFLINE=1 USAGE_CLAUDE_DATA="$CHECK/data" USAGE_CLAUDE_CACHE="$CHECK/cache"
/usr/bin/python3 -I -B "$RES/cli.py" page >/dev/null || { echo "bundle check failed: cli.py page" >&2; exit 1; }
/usr/bin/python3 -I -B -c 'import os, sys; sys.path.insert(0, sys.argv[1]); import build, core, pricing
prices, offline = pricing.load(core.cache_root())
assert prices and offline, "no price list in the bundle"
assert os.path.getsize(os.path.join(sys.argv[1], "vendor", "d3.min.js")) > 0, "no d3 in the bundle"' "$RES" \
  || { echo "bundle check failed: price list or d3 missing" >&2; exit 1; }
unset USAGE_CLAUDE_OFFLINE USAGE_CLAUDE_DATA USAGE_CLAUDE_CACHE
codesign --force -s - "$APP"

if [ "$INSTALL" = 0 ]; then
  echo "built: $APP"
  exit 0
fi
DEST="/Applications/Burnboard.app"
if [ -e "$DEST" ]; then mv "$DEST" "$HOME/.Trash/Burnboard $(date +%s).app"; fi
mv "$APP" "$DEST"; rmdir "$OUT_DIR"
echo "installed: $DEST"
