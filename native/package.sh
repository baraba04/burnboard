#!/bin/zsh
# Build the app and put it in dist/ twice: Burnboard-<version>.dmg, the download for people (a window with the
# app and a link to Applications, so the app is dragged to its place and never runs from Downloads), and
# Burnboard-<version>.zip, the app alone, which the app's updater downloads. Both hold the same bundle.
# Install and first-open steps are on the download page and in README.md.
# Also writes dist/release.json (version, zip URL, SHA-256, size, Ed25519 signature) that the app's updater
# checks; the signing key is the BURNBOARD_UPDATE_KEY line of .env (`release-tool keygen` makes it once).
# Installs nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' native/Info.plist)"
STAGE="$(mktemp -d)"
trap 'hdiutil detach -quiet -force "$STAGE/mnt" 2>/dev/null || true; rm -rf "$STAGE"' EXIT
swiftc -O -o "$STAGE/release-tool" native/release-tool.swift
# No key, or a key whose public half is not the updatePublicKey in native/main.swift (apps built with this
# source would not accept what it signs): stops here with one line, before the long build.
"$STAGE/release-tool" check-key >/dev/null
native/build-app.sh --out "$STAGE" >/dev/null
mkdir -p dist
ZIP="dist/Burnboard-$VERSION.zip"
rm -f "$ZIP"
# ditto keeps the code signature and the extended attributes of the bundle intact.
/usr/bin/ditto -c -k --keepParent "$STAGE/Burnboard.app" "$ZIP"
echo "made: $ZIP"
# The checksum goes next to the zip, so the person who gets it can compare it.
(cd dist && shasum -a 256 "${ZIP:t}" | tee "${ZIP:t}.sha256")
# The dmg: the same bundle, a link to /Applications and a fixed window layout (native/dmg-layout.dsstore: icon
# view, the app on the left, Applications on the right; made once, no Finder scripting at build time).
DMG="dist/Burnboard-$VERSION.dmg"
rm -f "$DMG"
mkdir "$STAGE/dmg"
/usr/bin/ditto "$STAGE/Burnboard.app" "$STAGE/dmg/Burnboard.app"
ln -s /Applications "$STAGE/dmg/Applications"
cp native/dmg-layout.dsstore "$STAGE/dmg/.DS_Store"
hdiutil create -quiet -volname Burnboard -srcfolder "$STAGE/dmg" -fs HFS+ -format UDZO "$DMG"
# Read back: the image mounts and the app inside still has a valid signature.
mkdir "$STAGE/mnt"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$STAGE/mnt" "$DMG"
codesign --verify --deep --strict "$STAGE/mnt/Burnboard.app" && [ -L "$STAGE/mnt/Applications" ] || { hdiutil detach -quiet "$STAGE/mnt"; echo "the dmg does not hold a valid app and the Applications link"; exit 1; }
hdiutil detach -quiet "$STAGE/mnt"
echo "made: $DMG"
(cd dist && shasum -a 256 "${DMG:t}" | tee "${DMG:t}.sha256")
"$STAGE/release-tool" sign --zip "$ZIP" --version "$VERSION" --out dist/release.json
# Verified against the app's own constant, not against .env.
"$STAGE/release-tool" verify --json dist/release.json --zip "$ZIP" --pubkey "$("$STAGE/release-tool" app-pubkey)"
