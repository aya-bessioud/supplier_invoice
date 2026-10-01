# Starts n8n at http://localhost:5678 with the settings from ..\.env
$root = Split-Path -Parent $PSScriptRoot
Get-Content (Join-Path $root ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process") }
}
$n8n = Join-Path $root "n8n-runtime\node_modules\.bin\n8n.cmd"
if (-not (Test-Path $n8n)) { Write-Error "n8n not installed: run 'npm install' in $root\n8n-runtime"; exit 1 }
New-Item -ItemType Directory -Force $env:N8N_USER_FOLDER | Out-Null
& $n8n start
