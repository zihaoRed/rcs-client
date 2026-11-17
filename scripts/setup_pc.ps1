$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Set-Location pc\agent
npm ci
npm run build
Write-Host "PC setup complete. Built pc/frida/token_harvester_bundle.js"
