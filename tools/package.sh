#!/usr/bin/env bash
#
# package.sh — build the installable zip for a GitHub release.
#
#   tools/package.sh            -> dist/com.austin.lottie2svg-<version>.zip
#
# The version comes from CSXS/manifest.xml, so there is one place to bump it.
# The zip contains the bundle folder, which is what people expect to drop into
# the CEP extensions folder if they would rather not run the installer.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_ID="$(sed -n 's/.*ExtensionBundleId="\([^"]*\)".*/\1/p' "$ROOT/CSXS/manifest.xml" | head -1)"
VERSION="$(sed -n 's/.*ExtensionBundleVersion="\([^"]*\)".*/\1/p' "$ROOT/CSXS/manifest.xml" | head -1)"
STAGE="$(mktemp -d)"
OUT="$ROOT/dist/$BUNDLE_ID-$VERSION.zip"

mkdir -p "$STAGE/$BUNDLE_ID" "$ROOT/dist"
for item in CSXS index.html js jsx py install.sh install.ps1 README.md LICENSE CHANGELOG.md; do
  [[ -e "$ROOT/$item" ]] && cp -R "$ROOT/$item" "$STAGE/$BUNDLE_ID/"
done
rm -rf "$STAGE/$BUNDLE_ID/py/__pycache__"
chmod +x "$STAGE/$BUNDLE_ID/install.sh"

rm -f "$OUT"
(cd "$STAGE" && zip -rq "$OUT" "$BUNDLE_ID" -x "*.DS_Store")
rm -rf "$STAGE"

printf '  wrote %s (%s)\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
