"""Prints the Invoices / AuditLog / KPI tabs of the Google Sheet (runs workflow 99_read_sheet through the n8n CLI).

    python scripts/read_sheet.py            # everything
    python scripts/read_sheet.py --last 3   # only the last 3 invoices and their audit rows
"""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INV_COLS = ["invoice_id", "invoice_number", "supplier_name", "gross_amount", "ai_confidence", "status", "route",
            "approved_by", "payment_status", "rejection_reasons", "archive_link", "received_at", "processed_at"]


def run_read():
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "n8n-cli.ps1"),
           "execute", "--id=invReadSheet0001", "--rawOutput"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    data = json.loads(out[out.index("{"):])
    if data.get("status") != "success":
        raise SystemExit("read workflow failed: " + json.dumps(data.get("data", {}).get("resultData", {}).get("error"))[:500])
    return data["data"]["resultData"]["runData"]["Summary"][0]["data"]["main"][0][0]["json"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=0)
    a = ap.parse_args()
    d = run_read()
    inv = d["invoices"][-a.last:] if a.last else d["invoices"]
    ids = {r.get("invoice_id") for r in inv}
    print(f"== Invoices ({len(d['invoices'])} rows, showing {len(inv)})")
    for r in inv:
        print(" | ".join(f"{c}={r.get(c, '')}" for c in INV_COLS if r.get(c, "") != ""))
    print("\n== AuditLog")
    for r in d["audit"]:
        if not a.last or r.get("invoice_id") in ids:
            print(f"{r.get('timestamp')} | {r.get('invoice_id')} | {r.get('event')} | {r.get('details')}")
    print("\n== KPI")
    for r in d["kpi"]:
        if r.get("KPI"):
            print(f"{r['KPI']:42s} {r.get('Value')}")


if __name__ == "__main__":
    main()
