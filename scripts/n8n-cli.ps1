# Runs an n8n CLI command with the settings from ..\.env
#   e.g.  .\scripts\n8n-cli.ps1 import:workflow --input=workflows\01_invoice_automation.json
$root = Split-Path -Parent $PSScriptRoot
Get-Content (Join-Path $root ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process") }
}
# the running server already uses the task-runner broker port 5679
$env:N8N_RUNNERS_BROKER_PORT = "5690"
& (Join-Path $root "n8n-runtime\node_modules\.bin\n8n.cmd") @args
exit $LASTEXITCODE
