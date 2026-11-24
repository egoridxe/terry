$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "[1/3] Creating virtual environment (.venv)..." -ForegroundColor Cyan
if (-not (Test-Path "$Root\.venv")) {
    python -m venv "$Root\.venv"
}

Write-Host "[2/3] Installing Python dependencies..." -ForegroundColor Cyan
& "$Root\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$Root\.venv\Scripts\python.exe" -m pip install -r "$Root\requirements.txt"

Write-Host "[3/3] Downloading offline Russian Vosk model (~45 MB)..." -ForegroundColor Cyan
$ModelsDir = Join-Path $Root "models"
$TargetDir = Join-Path $ModelsDir "vosk-model-small-ru-0.22"
$ZipPath   = Join-Path $ModelsDir "vosk-model-small-ru-0.22.zip"
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

if (-not (Test-Path $TargetDir)) {
    Invoke-WebRequest -Uri "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip" -OutFile $ZipPath -UseBasicParsing
    Expand-Archive -Path $ZipPath -DestinationPath $ModelsDir -Force
    Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
}

Write-Host "`nDone! Launch Terry by double-clicking 'Start Voice Media.vbs'." -ForegroundColor Green
