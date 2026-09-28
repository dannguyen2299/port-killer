#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

python3 -m pip install --user pyinstaller

python3 -m PyInstaller \
  --clean \
  --onefile \
  --name port-killer-macos \
  --add-data "index.html:." \
  app.py

chmod +x dist/port-killer-macos
echo "Built: $(pwd)/dist/port-killer-macos"
