#!/bin/zsh
# Build the app and put it in dist/Burnboard-<version>.zip: the app alone, so the download opens
# to one icon to double-click. Install and first-open steps are on the download page and in README.md.
# Installs nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' native/Info.plist)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
native/build-app.sh --out "$STAGE" >/dev/null
mkdir -p dist
ZIP="dist/Burnboard-$VERSION.zip"
rm -f "$ZIP"
# ditto keeps the code signature and the extended attributes of the bundle intact.
/usr/bin/ditto -c -k --keepParent "$STAGE/Burnboard.app" "$ZIP"
echo "made: $ZIP"
# The checksum goes next to the zip, so the person who gets it can compare it.
(cd dist && shasum -a 256 "${ZIP:t}" | tee "${ZIP:t}.sha256")
