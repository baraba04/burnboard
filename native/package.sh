#!/bin/zsh
# Build the app and put it in dist/Burnboard-<version>.zip: the app alone, so the download opens
# to one icon to double-click. Install and first-open steps are on the download page and in README.md.
# Also writes dist/release.json (version, zip URL, SHA-256, size, Ed25519 signature) that the app's updater
# checks; the signing key is the BURNBOARD_UPDATE_KEY line of .env (`release-tool keygen` makes it once).
# Installs nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' native/Info.plist)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
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
"$STAGE/release-tool" sign --zip "$ZIP" --version "$VERSION" --out dist/release.json
# Verified against the app's own constant, not against .env.
"$STAGE/release-tool" verify --json dist/release.json --zip "$ZIP" --pubkey "$("$STAGE/release-tool" app-pubkey)"
