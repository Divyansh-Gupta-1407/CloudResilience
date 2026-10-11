"""ResilienceAI's database: one SQLite file, out/resilienceai.db (Python's built-in sqlite3, no server).

Tables
  baseline            the healthy reference measurements (latest row is used)
  experiment_runs     every single experiment (manual / random / old planner): summary columns + full record
  squad_runs          every 3-agent run: status, resilience score + the full saved state
  events              the event log shown in History
  telemetry_windows   every 3-second measurement (requests, error rate, p50/p95, faults active)
  error_events        every error group seen by telemetry, attributed to a service
  memory_tests        the agents' long-term shared memory: one row per service x fault x run
  memory_runs         the agents' long-term shared memory: one row per analysed squad run
  plans               plans made by the original single AI planner

Open it with any SQLite viewer (e.g. "DB Browser for SQLite") or:  python -m chaos.db
On first use, the older JSON files in out/ are imported once, so no history is lost.
"""
import glob
import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
DB_PATH = OUT / "resilienceai.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL, requests INTEGER, error_rate REAL, p50_ms REAL, p95_ms REAL,
    data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY, created_at REAL, target TEXT, fault TEXT, status TEXT, stop_reason TEXT,
    planned_by TEXT, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS squad_runs (
    id TEXT PRIMARY KEY, started_at REAL, status TEXT, resilience_score INTEGER, analyst_model TEXT,
    state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, category TEXT, message TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS telemetry_windows (
    t REAL PRIMARY KEY, timestamp TEXT, requests INTEGER, error_rate REAL, p50_ms REAL, p95_ms REAL,
    stalled INTEGER, browse_error_rate REAL, cart_error_rate REAL, checkout_error_rate REAL, faults TEXT);
CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, ts TEXT, service TEXT, type TEXT, count INTEGER,
    attributed_by TEXT, message TEXT, journeys TEXT, suspected_cause TEXT);
CREATE TABLE IF NOT EXISTS memory_tests (
    run_id TEXT, service TEXT, fault TEXT, score INTEGER, severity TEXT, failure_rate REAL,
    pages_failed TEXT, at TEXT, PRIMARY KEY (run_id, service, fault));
CREATE TABLE IF NOT EXISTS memory_runs (
    run_id TEXT PRIMARY KEY, at TEXT, targets TEXT, resilience_score INTEGER);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, created_at REAL, status TEXT, model TEXT, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
CREATE INDEX IF NOT EXISTS idx_errors_service ON error_events(service, t);
CREATE INDEX IF NOT EXISTS idx_memory_service ON memory_tests(service, at);
"""

_lock = threading.Lock()
_local = threading.local()


def conn():
    """One connection per thread (sqlite3 connections must not be shared across threads)."""
    c = getattr(_local, "c", None)
    if c is None:
        OUT.mkdir(exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


def execute(sql, params=()):
    with _lock:
        c = conn()
        c.execute(sql, params)
        c.commit()


def query(sql, params=()):
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def _j(v):
    return json.dumps(v, default=str)


# ------------------------------------------------------------------ writers

def save_baseline(b):
    o = b.get("overall", {})
    execute("INSERT INTO baseline (captured_at, requests, error_rate, p50_ms, p95_ms, data) VALUES (?,?,?,?,?,?)",
            (b.get("captured_at"), o.get("n"), o.get("error_rate"), o.get("p50_ms"), o.get("p95_ms"), _j(b)))


def save_run(record):
    exp = record.get("experiment") or record.get("request") or {}
    execute("INSERT OR REPLACE INTO experiment_runs (id, created_at, target, fault, status, stop_reason, planned_by, record) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (record["run_id"], record.get("applied_at") or time.time(), exp.get("target"), exp.get("failure_type"),
             record.get("status"), record.get("stop_reason"),
             "AI agent" if exp.get("plan_id") else exp.get("planned_by") or "manual", _j(record)))


def save_squad(state):
    sv = state.get("severity") or {}
    ranked = sv.get("ranked") or []
    score = None
    if ranked:
        s = [e["formula_score"] for e in ranked]
        score = round(100 - (0.6 * max(s) + 0.4 * sum(s) / len(s)))
    execute("INSERT OR REPLACE INTO squad_runs (id, started_at, status, resilience_score, analyst_model, state) "
            "VALUES (?,?,?,?,?,?)", (state["id"], state.get("started"), state.get("status"), score, sv.get("model"), _j(state)))


def save_event(e):
    extra = {k: v for k, v in e.items() if k not in ("timestamp", "category", "event")}
    execute("INSERT INTO events (ts, category, message, extra) VALUES (?,?,?,?)",
            (e["timestamp"], e["category"], e["event"], _j(extra) if extra else None))


def save_window(row):
    execute("INSERT OR REPLACE INTO telemetry_windows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["t"], row["timestamp"], row["n"], row["error_rate"], row["p50_ms"], row["p95_ms"], row.get("stalled"),
             row.get("browse_error_rate"), row.get("cart_error_rate"), row.get("checkout_error_rate"),
             _j(row.get("faults") or [])))


def save_error(e):
    execute("INSERT INTO error_events (t, ts, service, type, count, attributed_by, message, journeys, suspected_cause) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (e["t"], e["timestamp"], e["service"], e["type"], e["count"], e.get("attributed_by"), e.get("message"),
             _j(e.get("journeys", [])), e.get("suspected_cause")))


def save_plan(record):
    execute("INSERT OR REPLACE INTO plans (id, created_at, status, model, record) VALUES (?,?,?,?,?)",
            (record["plan_id"], time.time(), record.get("status"), record.get("model"), _j(record)))


def save_memory_test(run_id, t):
    execute("INSERT OR IGNORE INTO memory_tests VALUES (?,?,?,?,?,?,?,?)",
            (run_id, t["service"], t["fault"], t.get("score"), t.get("severity"), t.get("failure_rate"),
             _j(t.get("pages_failed", [])), t["at"]))


def save_memory_run(run_id, at, targets, score):
    execute("INSERT OR REPLACE INTO memory_runs VALUES (?,?,?,?)", (run_id, at, _j(targets), score))


# ------------------------------------------------------------------ readers

def latest_baseline():
    r = query("SELECT data FROM baseline ORDER BY id DESC LIMIT 1")
    return json.loads(r[0]["data"]) if r else None


def runs(limit=50):
    return [json.loads(r["record"]) for r in
            query("SELECT record FROM experiment_runs ORDER BY created_at DESC LIMIT ?", (limit,))]


def squads(limit=20):
    return [json.loads(r["state"]) for r in query("SELECT state FROM squad_runs ORDER BY id DESC LIMIT ?", (limit,))]


def recent_events(limit=500):
    out = []
    for r in query("SELECT ts, category, message, extra FROM events ORDER BY id DESC LIMIT ?", (limit,)):
        out.append({"timestamp": r["ts"], "category": r["category"], "event": r["message"],
                    **(json.loads(r["extra"]) if r["extra"] else {})})
    return out


def latest_plan():
    r = query("SELECT record FROM plans ORDER BY created_at DESC LIMIT 1")
    return json.loads(r[0]["record"]) if r else None


def memory_tests(service=None):
    sql = "SELECT * FROM memory_tests" + (" WHERE service = ?" if service else "") + " ORDER BY at"
    rows = query(sql, (service,) if service else ())
    for r in rows:
        r["pages_failed"] = json.loads(r["pages_failed"] or "[]")
        r["run"] = r.pop("run_id")
    return rows


def memory_runs():
    rows = query("SELECT * FROM memory_runs ORDER BY at")
    for r in rows:
        r["targets"] = json.loads(r["targets"] or "[]")
        r["run"] = r.pop("run_id")
    return rows


def table_counts():
    names = [r["name"] for r in query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: query(f"SELECT COUNT(*) AS n FROM {n}")[0]["n"] for n in names}


# ------------------------------------------------------------------ setup + one-time import of the old JSON files

def _import_json():
    def load(path):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None

    b = load(OUT / "baseline.json")
    if b and not query("SELECT 1 FROM baseline LIMIT 1"):
        save_baseline(b)
    for p in sorted(glob.glob(str(OUT / "runs" / "*.json"))):
        r = load(p)
        if r and r.get("run_id"):
            save_run(r)
    for p in sorted(glob.glob(str(OUT / "squads" / "*.json"))):
        q = load(p)
        if q and q.get("id"):
            save_squad(q)
    for p in sorted(glob.glob(str(OUT / "plans" / "*.json"))):
        r = load(p)
        if r and r.get("plan_id"):
            save_plan(r)
    m = load(OUT / "memory" / "store.json")
    if m:
        for service, v in (m.get("services") or {}).items():
            for t in v.get("tests", []):
                save_memory_test(t["run"], {**t, "service": service})
        for run_id, v in (m.get("runs") or {}).items():
            save_memory_run(run_id, v.get("at"), v.get("targets", []), v.get("resilience_score"))


def init():
    with _lock:
        c = conn()
        fresh = not c.execute("SELECT 1 FROM sqlite_master WHERE name='squad_runs'").fetchone()
        c.executescript(SCHEMA)
        c.commit()
    if fresh:
        _import_json()


init()


# ------------------------------------------------------------------ cleanup

# Tables that only grow with live traffic, and the time column used to prune them.
_TIMED = {"telemetry_windows": "t", "error_events": "t"}
# Tables the cleanup command may empty. baseline is excluded: experiments refuse to run without it.
CLEARABLE = ("telemetry_windows", "error_events", "events", "experiment_runs", "squad_runs", "plans",
             "memory_tests", "memory_runs")


def backup():
    """Copy the database to out/backups/resilienceai-<time>.db before any cleanup."""
    dest = OUT / "backups" / f"resilienceai-{time.strftime('%Y%m%d-%H%M%S')}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        src = conn()
        out = sqlite3.connect(dest)
        src.backup(out)
        out.close()
    return dest


def prune(days):
    """Delete live telemetry, error events and events older than `days` days. Runs and memory are kept."""
    cutoff = time.time() - days * 86400
    iso_cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    deleted = {}
    with _lock:
        c = conn()
        for table, col in _TIMED.items():
            deleted[table] = c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
        deleted["events"] = c.execute("DELETE FROM events WHERE ts < ?", (iso_cutoff,)).rowcount
        c.commit()
    return deleted


def clear(table):
    if table not in CLEARABLE:
        raise ValueError(f"{table!r} cannot be cleared; choose from {', '.join(CLEARABLE)}")
    with _lock:
        c = conn()
        n = c.execute(f"DELETE FROM {table}").rowcount
        c.commit()
    return n


def delete_squad(run_id):
    """Remove one squad run and what the agents remembered from it."""
    with _lock:
        c = conn()
        n = {"squad_runs": c.execute("DELETE FROM squad_runs WHERE id = ?", (run_id,)).rowcount,
             "memory_tests": c.execute("DELETE FROM memory_tests WHERE run_id = ?", (run_id,)).rowcount,
             "memory_runs": c.execute("DELETE FROM memory_runs WHERE run_id = ?", (run_id,)).rowcount}
        c.commit()
    return n


def vacuum():
    """Give the freed space back to the disk."""
    with _lock:
        c = conn()
        c.commit()
        c.execute("VACUUM")


def _cli():
    import argparse
    ap = argparse.ArgumentParser(prog="python -m chaos.db", description="Inspect and clean the ResilienceAI database.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stats", help="row count of every table and the file size (default)")
    p = sub.add_parser("prune", help="delete telemetry, error events and events older than N days (runs and memory kept)")
    p.add_argument("--days", type=float, default=1)
    p = sub.add_parser("clear", help="empty one table")
    p.add_argument("table", choices=CLEARABLE)
    p = sub.add_parser("delete-squad", help="delete one squad run and the agents' memory of it")
    p.add_argument("run_id")
    sub.add_parser("vacuum", help="shrink the file after deleting")
    sub.add_parser("backup", help="copy the database to out/backups")
    a = ap.parse_args()

    if a.cmd in ("prune", "clear", "delete-squad"):
        print(f"backup: {backup()}")
    if a.cmd == "prune":
        print("deleted:", prune(a.days))
    elif a.cmd == "clear":
        print(f"deleted {clear(a.table)} rows from {a.table}")
    elif a.cmd == "delete-squad":
        print("deleted:", delete_squad(a.run_id))
    elif a.cmd == "vacuum":
        vacuum()
    elif a.cmd == "backup":
        print(f"backup: {backup()}")
    if a.cmd in ("prune", "clear", "delete-squad"):
        vacuum()
        print("Restart the backend so it reloads the agents' memory if you changed memory tables.")
    print(f"\n{DB_PATH}  ({DB_PATH.stat().st_size / 1024:.0f} KB)")
    for name, n in table_counts().items():
        print(f"  {name:<20} {n} rows")


if __name__ == "__main__":
    _cli()