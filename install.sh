#!/usr/bin/env bash
#
# install.sh — put the panel where After Effects looks for it, and build the
# private python environment it shells out to.
#
#   ./install.sh            install / upgrade
#   ./install.sh --uninstall
#
# Nothing here touches system python or any other Adobe extension. Re-running
# upgrades in place.

set -euo pipefail

BUNDLE_ID="com.austin.lottie2svg"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPPORT="$HOME/.lottie2svg"
VENV="$SUPPORT/venv"
BIN="$SUPPORT/bin"
CEP="$HOME/Library/Application Support/Adobe/CEP/extensions"
DEST="$CEP/$BUNDLE_ID"

say() { printf '  %s\n' "$*"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$DEST"
  say "removed $DEST"
  say "the python environment at $SUPPORT was left alone; delete it by hand if you want it gone"
  exit 0
fi

# ---------------------------------------------------------------- python --- #
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "python3 not found. Install it (xcode-select --install, or python.org) and re-run." >&2
  exit 1
fi

say "building the python environment in $VENV"
mkdir -p "$SUPPORT"
if [[ ! -x "$VENV/bin/python3" ]]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python3" -m pip install --quiet --upgrade pip
"$VENV/bin/python3" -m pip install --quiet --upgrade lottie

# ------------------------------------------------------------------ panel --- #
say "installing the panel into $DEST"
mkdir -p "$CEP"
rm -rf "$DEST"
mkdir -p "$DEST"
for item in CSXS index.html js jsx py README.md; do
  [[ -e "$SRC/$item" ]] && cp -R "$SRC/$item" "$DEST/"
done

# ---------------------------------------------- unsigned extensions (CEP) --- #
# Any locally built panel is unsigned, so every CEP version in play has to be
# told to load it. Harmless if a version is not installed.
for v in 9 10 11 12 13; do
  defaults write "com.adobe.CSXS.$v" PlayerDebugMode 1 2>/dev/null || true
done
say "allowed unsigned extensions (PlayerDebugMode)"

# -------------------------------------------------------------------- cli --- #
mkdir -p "$BIN"
cat > "$BIN/l2s" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python3" "$DEST/py/l2s.py" "\$@"
EOF
chmod +x "$BIN/l2s"
say "wrote the l2s command to $BIN/l2s"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "add it to your PATH:  export PATH=\"\$PATH:$BIN\"" ;;
esac

echo
say "Done. Quit After Effects fully and reopen it, then:"
say "Window > Extensions > SVG + Lottie Export"
