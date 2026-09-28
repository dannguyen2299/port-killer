$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

python -m pip install --user pyinstaller

python -m PyInstaller `
  --clean `
  --onefile `
  --name port-killer-windows `
  --add-data "index.html;." `
  app.py

Write-Host "Built: $PSScriptRoot\dist\port-killer-windows.exe"
