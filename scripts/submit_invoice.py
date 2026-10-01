"""Submits an invoice through the n8n form (same HTTP request as the browser) and waits for its processing execution.

    python scripts/submit_invoice.py samples/facture_01_auto_valide.pdf [--email you@gmail.com]
"""
import argparse
import mimetypes
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "storage" / "n8n" / ".n8n" / "database.sqlite"
FORM = "http://localhost:5678/form/invoice-upload"


def max_exec_id():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        return db.execute("select coalesce(max(id),0) from execution_entity").fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--email", default="kharratok@gmail.com")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    f = Path(a.file)
    before = max_exec_id()
    mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    r = requests.post(FORM, files={"field-0": (f.name, f.read_bytes(), mime)}, data={"field-1": a.email}, timeout=60)
    print(f"form POST {f.name} -> HTTP {r.status_code} {r.text[:120]}")
    t0 = time.time()
    while time.time() - t0 < a.timeout:
        time.sleep(3)
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
            row = db.execute("select id,status from execution_entity where id>? and workflowId='invAutomation001' "
                             "and mode='integrated' order by id limit 1", (before,)).fetchone()
        if row and row[1] not in ("new", "running"):
            print(f"processing execution #{row[0]} -> {row[1]} after {time.time() - t0:.0f}s")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "show_executions.py"), "--id", str(row[0])] +
                           (["--nodes"] if row[1] == "error" else []))
            return
    print("timeout waiting for the processing execution")


if __name__ == "__main__":
    main()
