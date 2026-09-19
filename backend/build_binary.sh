#!/bin/bash
# Compila el binario standalone (sin Python) con PyInstaller para el SO actual.
# Salida en descargas/DiagraMinder-Backend-<os>[.exe].
#
# Uso (en una venv o con pyinstaller instalado):
#   pip install pyinstaller certifi
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

# --collect-data certifi: sin su cacert.pem adentro, TODO HTTPS del binario falla con
# CERTIFICATE_VERIFY_FAILED (el chequeo de versiones, entre otros). El `import certifi`
# vive dentro de un try, así que PyInstaller no lo detecta solo.
# --noconsole en Windows: es una app, no un script; una terminal negra la hace ver rota.
VENTANA=""; [ "$OSNAME" = "win" ] && VENTANA="--noconsole"
pyinstaller --onefile $VENTANA --name "DiagraMinder-Backend-$OSNAME" \
  --hidden-import certifi --collect-data certifi \
  --distpath "$OUT" --workpath /tmp/dmwork --specpath /tmp/dmspec \
  "$HERE/server.py"

echo "Generado: $OUT/DiagraMinder-Backend-$OSNAME"
