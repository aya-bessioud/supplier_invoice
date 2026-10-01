"""Shows the latest n8n executions (read-only access to the local n8n SQLite database).

    python scripts/show_executions.py              # last 5 executions of 01_invoice_automation
    python scripts/show_executions.py --last 10 --nodes   # + per-node item counts
    python scripts/show_executions.py --id 42 --node "Validate extraction"   # dump one node's output
"""
import argparse
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "storage" / "n8n" / ".n8n" / "database.sqlite"


def unflatten(text):
    """Decode n8n's 'flatted' execution-data format."""
    arr, cache = json.loads(text), {}

    def res(i):
        v = arr[i]
        if isinstance(v, str) or not isinstance(v, (list, dict)):
            return v
        if i in cache:
            return cache[i]
        if isinstance(v, list):
            out = cache[i] = []
            out.extend(res(int(x)) if isinstance(x, str) else x for x in v)
        else:
            out = cache[i] = {}
            for k, x in v.items():
                out[k] = res(int(x)) if isinstance(x, str) else x
        return out
    return res(0)


def node_items(run):
    main = (run.get("data") or {}).get("main") or []
    return [len(o or []) for o in main]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default="invAutomation001")
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--id", type=int)
    ap.add_argument("--nodes", action="store_true")
    ap.add_argument("--node")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    if a.id:
        rows = db.execute("select e.id,e.mode,e.status,e.startedAt,e.stoppedAt,d.data from execution_entity e "
                          "join execution_data d on d.executionId=e.id where e.id=?", (a.id,)).fetchall()
    else:
        rows = db.execute("select e.id,e.mode,e.status,e.startedAt,e.stoppedAt,d.data from execution_entity e "
                          "join execution_data d on d.executionId=e.id where e.workflowId=? order by e.id desc limit ?",
                          (a.workflow, a.last)).fetchall()[::-1]
    for eid, mode, status, start, stop, data in rows:
        d = unflatten(data)
        rd = d.get("resultData", {})
        run = rd.get("runData", {})
        err = rd.get("error")
        last = rd.get("lastNodeExecuted")
        print(f"#{eid} mode={mode} status={status} start={start} stop={stop} last_node={last}")
        if err:
            print(f"   ERROR: {err.get('message')} {err.get('description') or ''}"[:400])
        if a.nodes:
            for n, runs in run.items():
                r = runs[-1]
                e = r.get("error")
                print(f"   - {n:45s} out={node_items(r)} {('ERR: ' + str(e.get('message'))[:150]) if e else ''}")
        if a.node and a.node in run:
            print(json.dumps([i.get("json") for o in (run[a.node][-1].get("data") or {}).get("main", []) for i in (o or [])],
                             indent=1, ensure_ascii=False)[:6000])
        for n in run:
            if n.startswith("Result"):
                print("   RESULT:", json.dumps(run[n][-1]["data"]["main"][0][0]["json"], ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
