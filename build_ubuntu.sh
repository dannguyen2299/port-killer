#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

pyinstaller \
  --clean \
  --onefile \
  --name port-killer-ubuntu \
  --add-data "index.html:." \
  app.py

chmod +x dist/port-killer-ubuntu
echo "Built: $(pwd)/dist/port-killer-ubuntu"
