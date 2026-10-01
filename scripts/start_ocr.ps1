# Starts the local OCR service at http://localhost:8000 (POST /extract, GET /health)
$root = Split-Path -Parent $PSScriptRoot
$ocr  = Join-Path $root "ocr"
$py   = Join-Path $ocr ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv (Join-Path $ocr ".venv")
    & $py -m pip install -r (Join-Path $ocr "requirements.txt")
}
$env:TESSDATA_PREFIX = Join-Path $ocr "tessdata"
Set-Location $ocr
& $py -m uvicorn app:app --host 127.0.0.1 --port 8000
