#!/bin/bash
# Compila el binario standalone (sin Python) con PyInstaller para el SO actual.
# Salida en descargas/DiagraMinder-Backend-<os>[.exe].
#
# Uso (en una venv o con pyinstaller instalado):
#   pip install pyinstaller
#   bash backend/build_binary.sh
#
# Cada SO compila su propio binario (Windows en Windows, etc.). El workflow
# .github/workflows/build-backend.yml hace los 3 automáticamente.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/descargas"
mkdir -p "$OUT"

case "$(uname -s)" in
  Darwin*) OSNAME="mac" ;;
  Linux*)  OSNAME="linux" ;;
  MINGW*|MSYS*|CYGWIN*) OSNAME="win" ;;
  *) OSNAME="unknown" ;;
esac

pyinstaller --onefile --name "DiagraMinder-Backend-$OSNAME" \
  --distpath "$OUT" --workpath /tmp/dmwork --specpath /tmp/dmspec \
  "$HERE/server.py"

echo "Generado: $OUT/DiagraMinder-Backend-$OSNAME"
