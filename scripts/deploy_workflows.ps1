# Rebuilds the workflow JSON from .env, imports it into n8n and publishes the main workflow.
# Stop n8n first (Ctrl+C in its window), run this script, then start n8n again (scripts\start_n8n.ps1).
$root = Split-Path -Parent $PSScriptRoot
$cli  = Join-Path $PSScriptRoot "n8n-cli.ps1"
python (Join-Path $root "workflows\build_workflows.py")
foreach ($wf in "00_setup_google_sheet", "01_invoice_automation", "97_sheet_guide", "98_reset_test_data", "99_read_sheet") {
    $file = Join-Path $root "workflows\$wf.json"
    & $cli import:workflow "--input=$file"
}
& $cli publish:workflow --id=invAutomation001
