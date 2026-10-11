"""Long-term memory shared by all four agents, as a LangGraph Store backed by the database.

Namespaces in the Store (what the graph reads):
  ("chaos", "services")  key = service name -> {"tests": [ {run, fault, score, severity, failure_rate, pages_failed, at} ]}
  ("chaos", "runs")      key = run id       -> {"targets": [...], "resilience_score", "at"}

Persistence: database tables memory_tests and memory_runs (see db.py). The Store is loaded from them at
startup and every new run is written to both, so the memory survives backend restarts.
"""
import threading
import time

from langgraph.store.memory import InMemoryStore

from . import db

SERVICES = ("chaos", "services")
RUNS = ("chaos", "runs")
MAX_TESTS_PER_SERVICE = 20

STORE = InMemoryStore()
_lock = threading.Lock()


def _put_test(service, test):
    item = STORE.get(SERVICES, service)
    tests = item.value["tests"] if item else []
    if any(t["run"] == test["run"] and t["fault"] == test["fault"] for t in tests):
        return False
    tests.append(test)
    STORE.put(SERVICES, service, {"tests": tests[-MAX_TESTS_PER_SERVICE:]})
    return True


def load():
    with _lock:
        for t in db.memory_tests():
            service = t.pop("service")
            _put_test(service, t)
        for r in db.memory_runs():
            STORE.put(RUNS, r.pop("run"), r)


def remember_run(run_id, ranked, resilience_score):
    """Write one analysed run into long-term memory (called by the graph's 'remember' node)."""
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        for f in ranked:
            imp = f.get("impact") or {}
            test = {"run": run_id, "fault": f["failure_type"], "score": f.get("formula_score"),
                    "severity": f.get("severity"), "failure_rate": imp.get("failure_rate"),
                    "pages_failed": imp.get("pages_failed", []), "at": at}
            if _put_test(f["target"], test):
                db.save_memory_test(run_id, {**test, "service": f["target"]})
        targets = [f["target"] for f in ranked]
        STORE.put(RUNS, run_id, {"targets": targets, "at": at, "resilience_score": resilience_score})
        db.save_memory_run(run_id, at, targets, resilience_score)


def recall(services):
    """What the agents remember about these services: how often each was tested, how, and how badly it broke."""
    out = {}
    for s in services:
        item = STORE.get(SERVICES, s)
        tests = item.value["tests"] if item else []
        if not tests:
            out[s] = {"times_tested": 0}
            continue
        worst = max(tests, key=lambda t: t.get("score") or 0)
        out[s] = {"times_tested": len(tests),
                  "faults_tried": sorted({t["fault"] for t in tests}),
                  "worst": {"fault": worst["fault"], "score": worst.get("score"), "severity": worst.get("severity")},
                  "last": {"fault": tests[-1]["fault"], "score": tests[-1].get("score"), "at": tests[-1]["at"]}}
    return out


def history_for(service, fault=None):
    item = STORE.get(SERVICES, service)
    tests = item.value["tests"] if item else []
    return [t for t in tests if fault is None or t["fault"] == fault][-5:]


def summary():
    services = {i.key: len(i.value["tests"]) for i in STORE.search(SERVICES, limit=1000)}
    runs = sorted((i.value | {"run": i.key} for i in STORE.search(RUNS, limit=1000)), key=lambda r: r["at"] or "")
    return {"services_tested": services, "runs_remembered": len(runs), "recent_runs": runs[-5:]}


load()
