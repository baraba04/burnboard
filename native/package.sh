#!/bin/zsh
# Build the app and put it in dist/Burnboard-<version>.dmg: the app, a link to /Applications
# and "READ ME FIRST.txt" (the normal drag-to-install layout). Installs nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' native/Info.plist)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
native/build-app.sh --out "$STAGE" >/dev/null
ln -s /Applications "$STAGE/Applications"
cp native/READ-ME-FIRST.txt "$STAGE/READ ME FIRST.txt"
mkdir -p dist
DMG="dist/Burnboard-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "Burnboard" -srcfolder "$STAGE" -format UDZO -ov "$DMG"
echo "made: $DMG"
# The checksum goes next to the dmg, so the person who gets it can compare it (see READ-ME-FIRST.txt).
(cd dist && shasum -a 256 "${DMG:t}" | tee "${DMG:t}.sha256")
