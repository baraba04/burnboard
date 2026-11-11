#!/bin/zsh
# Build Burnboard from this source and install it to /Applications.
# An app built on this Mac is not blocked by macOS, so no "Open Anyway" step is needed.
set -euo pipefail
cd "$(dirname "$0")"
if ! xcode-select -p >/dev/null 2>&1; then
  echo "The Xcode command line tools are needed to build the app and to run it (Python 3)." >&2
  echo "Run: xcode-select --install" >&2
  echo "When that has finished, run this script again." >&2
  exit 1
fi
native/build-app.sh
echo "Open it from /Applications. It also needs Claude Code; the first screen checks it and has a button for whatever is missing."
