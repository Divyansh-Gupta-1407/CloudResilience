"""Backend API for the ResilienceAI dashboard (dashboard/app.py).

Run:  python -m uvicorn chaos.api:app --host 127.0.0.1 --port 8000

Every fault still goes through controller.run_experiment (validation, steady-state check,
automatic abort, kill switch, cleanup, recovery check). This module only schedules work
(one experiment or agent run at a time), exposes live telemetry, and implements rollback.
"""
import collections
import glob
import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import controller, db, squad
from .controller import OUT, ValidationError, kill_switch_path, load_safety, run_experiment, validate
from .discovery import DEFAULT_MANIFEST, discover
from .faults import CATALOG, Kube, find_faults, reset_all
from .planner import backend, latest_plan, make_plan, run_plan
from .telemetry import Ingestor

# Dashboard fault names -> controller fault templates.
FAULT_TYPES = {"latency": "network-delay", "pod_failure": "pod-kill", "service_unavailability": "service-down"}

app = FastAPI(title="ResilienceAI backend")
# The React frontend (frontend/, Vite dev server) runs on another origin.
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
cfg = load_safety()
kube = Kube(cfg["cluster_context"], cfg["namespace"])
ingestor = Ingestor()
events = collections.deque(db.recent_events(500), maxlen=500)   # newest first; full log in the database
_job_lock = threading.Lock()
job = {"id": None, "kind": None, "status": "IDLE"}   # the one background job (experiment / agent / plan)


def _now_iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).isoformat(timespec="seconds")


def log_event(message, category="INFO", **extra):
    _add_event({"timestamp": _now_iso(), "category": category, "event": message, **extra})


def _add_event(e):
    events.appendleft(e)
    try:
        db.save_event(e)
    except Exception:  # noqa: BLE001 - logging must never break an action
        pass


_CATEGORY = {"fault applied": "EXPERIMENT", "fault applying": "EXPERIMENT", "THRESHOLD BREACH": "SAFETY",
             "REJECTED": "SAFETY", "REFUSED": "SAFETY", "ERROR": "ERROR", "fault removed": "RECOVERY",
             "recovered": "RECOVERY", "NOT RECOVERED": "ERROR"}


def _on_controller_event(run_id, e):
    details = {k: v for k, v in e.items() if k not in ("t", "event")}
    text = e["event"] + (f" - {json.dumps(details, default=str)}" if details else "")
    _add_event({"timestamp": _now_iso(e["t"]), "category": _CATEGORY.get(e["event"], "INFO"),
                "event": text, "run_id": run_id})


controller.EVENT_HOOKS.append(_on_controller_event)


def _desired_replicas():
    docs = [d for d in yaml.safe_load_all(Path(DEFAULT_MANIFEST).read_text()) if d]
    return {d["metadata"]["name"]: d["spec"].get("replicas", 1) for d in docs if d.get("kind") == "Deployment"}


_faults_cache = {"t": 0, "faults": []}
_DESIRED = None


def _refresh_faults():
    global _DESIRED
    _DESIRED = _DESIRED or _desired_replicas()
    try:
        _faults_cache["faults"] = [{k: f[k] for k in ("service", "fault", "detail")}
                                   for f in find_faults(kube, cfg["allowed_targets"], _DESIRED)]
        _faults_cache["t"] = time.time()
    except (RuntimeError, OSError, ValueError):
        pass


def active_faults(max_age_s=None):
    """Faults currently present in the cluster. Normally served from the background refresher's
    cache (so /status answers instantly); max_age_s=0 forces a fresh check."""
    if max_age_s is not None and time.time() - _faults_cache["t"] > max_age_s:
        _refresh_faults()
    return _faults_cache["faults"]


def _fault_watcher():
    while True:
        _refresh_faults()
        time.sleep(3)


threading.Thread(target=_fault_watcher, daemon=True).start()


def _start_job(kind, fn):
    """Run fn() in the background unless another job is active. Returns the job id."""
    with _job_lock:
        if job["status"] in ("RUNNING", "PLANNING"):
            raise HTTPException(409, f"a {job['kind']} job is already running ({job['id']})")
        job.clear()
        job.update(id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, status="RUNNING", started=time.time(),
                   result=None, error=None)
        jid = job["id"]

    def wrapper():
        try:
            job["result"] = fn()
            job["status"] = "DONE"
        except Exception as e:  # noqa: BLE001 - report every failure to the dashboard
            job.update(status="FAILED", error=str(e))
            log_event(f"{kind} job failed: {e}", "ERROR")
        job["finished"] = time.time()

    threading.Thread(target=wrapper, daemon=True).start()
    return jid


def _summary(rec):
    exp = rec.get("experiment") or rec.get("request") or {}
    return {"id": rec.get("run_id"), "timestamp": rec.get("run_id", "")[:15],
            "target": exp.get("target"), "fault": exp.get("failure_type"), "params": exp.get("params"),
            "duration_seconds": exp.get("duration_s"), "objective": exp.get("hypothesis"),
            "status": rec.get("status"), "stop_reason": rec.get("stop_reason"),
            "cleanup": rec.get("cleanup_status"),
            "error_rate_during": (rec.get("during_fault") or {}).get("error_rate"),
            "p95_ms_during": (rec.get("during_fault") or {}).get("p95_ms"),
            "recovered": (rec.get("recovery") or {}).get("recovered"),
            "recovery_time_s": (rec.get("recovery") or {}).get("recovery_time_s"),
            "planned_by": ("AI agent" if (rec.get("request") or {}).get("plan_id")
                           else "random" if (rec.get("request") or {}).get("reason") == "chosen at random"
                           else "manual"),
            "reason": rec.get("reason")}


# ------------------------------------------------------------------ dashboard page (AJAX, polls the JSON API)

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).with_name("static") / "dashboard.html")


# ------------------------------------------------------------------ status & telemetry (read-only)

@app.get("/status")
def status():
    run = controller.CURRENT["run"]
    faults = active_faults()
    sq = squad.STATE["status"] if job.get("kind") == "squad" else None
    if squad.STATE.get("paused") and squad.is_active():
        state = "SQUAD PAUSED (LangGraph checkpoint) - press Continue"
    elif run:
        state = "HOLDING (until rollback)" if (run.get("experiment") or {}).get("hold") else "RUNNING"
    elif sq in ("PLANNING", "INJECTING") and job["status"] == "RUNNING":
        state = "SQUAD CHOOSING ERRORS" if sq == "PLANNING" else "SQUAD INJECTING"
    elif sq == "HOLDING" and job["status"] == "RUNNING":
        state = f"SQUAD HOLDING {len(controller.CURRENT.get('squad') or [])} ERRORS (until rollback)"
    elif job["status"] == "PLANNING":
        state = "AI PLANNING"
    elif faults:
        state = "FROZEN (kill switch) - press rollback"
    else:
        state = "IDLE"
    return {
        "ingestion": "RUNNING" if ingestor.running else "STOPPED",
        "experiment_status": state,
        "active_faults": faults,
        "current_experiment": {"run_id": run["run_id"], **(run.get("experiment") or run["request"])} if run else None,
        "job": {k: job.get(k) for k in ("id", "kind", "status", "error", "started", "finished")},
        "kill_switch": kill_switch_path(cfg).exists(),
        "baseline": ingestor.baseline,
        "ai_backend": backend(),
        "guardrail": {"error_rate": cfg["guardrail_error_rate"], "browse_error_rate": cfg["guardrail_browse_error_rate"],
                      "duration_s": cfg["guardrail_duration_s"], "armed": bool(faults) and ingestor.running,
                      "breaching_for_s": round(time.time() - GUARD["breach_since"]) if GUARD["breach_since"] else 0,
                      "rolling_back": _rollback_lock.locked(), "last_triggered": GUARD["last_triggered"]},
    }


@app.get("/metrics")
def metrics(limit: int = 200):
    rows = list(ingestor.metrics)[-limit:]
    return {"baseline": ingestor.baseline, "rows": rows, "health": ingestor.health()}


@app.get("/errors")
def errors(limit: int = 200):
    """Active = seen in the last ACTIVE_ERROR_S seconds. 'failed' counts requests that errored;
    'slow' counts requests that succeeded but took longer than the latency limit."""
    from .telemetry import ACTIVE_ERROR_S
    active = ingestor.active_errors()
    cutoff = time.time() - ACTIVE_ERROR_S
    total = sum(r["n"] for r in ingestor.metrics if r["t"] >= cutoff)
    def bucket(e):
        if e["type"] == "High latency":
            return "slow_requests"
        if e["type"].startswith("Degraded"):
            return "degraded_requests"
        return "failed_requests"

    failed = sum(e["count"] for e in active if bucket(e) == "failed_requests")
    slow = sum(e["count"] for e in active if bucket(e) == "slow_requests")
    degraded = sum(e["count"] for e in active if bucket(e) == "degraded_requests")
    by_service = {}
    for e in sorted(active, key=lambda e: e["t"]):
        s = by_service.setdefault(e["service"], {"service": e["service"], "failed_requests": 0,
                                                 "slow_requests": 0, "degraded_requests": 0, "types": set()})
        s[bucket(e)] += e["count"]
        s["types"].add(e["type"])
        s.update(last_seen=e["timestamp"], last_message=e["message"], attributed_by=e.get("attributed_by"),
                 suspected_cause=e["suspected_cause"])
    summary = [{**s, "types": ", ".join(sorted(s["types"]))}
               for s in sorted(by_service.values(),
                               key=lambda s: -(s["failed_requests"] + s["slow_requests"] + s["degraded_requests"]))]
    return {"window_s": ACTIVE_ERROR_S, "total_requests": total, "failed_requests": failed,
            "slow_requests": slow, "degraded_requests": degraded,
            "affected_services": [s["service"] for s in summary], "by_service": summary,
            "active": active, "history": list(ingestor.errors)[-limit:][::-1]}


@app.get("/events")
def get_events(limit: int = 200):
    return list(events)[:limit]


@app.get("/experiments/history")
def history(limit: int = 50):
    return [_summary(r) for r in db.runs(limit)]


@app.get("/services")
def services():
    dep = discover(DEFAULT_MANIFEST)
    try:
        items = json.loads(kube.run("get", "deployments", "-o", "json"))["items"]
        ready = {d["metadata"]["name"]: (d["status"].get("readyReplicas") or 0, d["spec"].get("replicas", 1))
                 for d in items}
    except RuntimeError:
        ready = {}
    out = []
    for name, s in dep["services"].items():
        if not s.get("deployed"):
            continue
        r, want = ready.get(name, (None, None))
        out.append({"service": name, "ready": f"{r}/{want}" if r is not None else "?",
                    "healthy": r is not None and want is not None and r >= want and want > 0,
                    "critical": s.get("critical"),
                    "calls": ", ".join(s["calls"]), "callers": ", ".join(s["callers"])})
    return out


# ------------------------------------------------------------------ ingestion

@app.post("/ingestion/start")
def ingestion_start():
    ingestor.start()
    log_event("Telemetry ingestion started (live probe traffic through the shop frontend).")
    return {"status": "RUNNING"}


@app.post("/ingestion/stop")
def ingestion_stop():
    ingestor.stop()
    log_event("Telemetry ingestion stopped. Recorded history retained.")
    return {"status": "STOPPED"}


# ------------------------------------------------------------------ manual experiments

class ExperimentRequest(BaseModel):
    target_service: str
    fault_type: str
    hold: bool = Field(True, description="keep the fault active until rollback / kill switch")
    duration_seconds: Optional[int] = Field(None, description="only used when hold is false")
    objective: str = ""
    delay_ms: Optional[int] = Field(None, description="latency faults only")


def _require_initial_state():
    """New faults are only injected into a clean system: a frozen fault must be rolled back first."""
    faults = active_faults(max_age_s=0)
    if faults:
        names = ", ".join(f"{f['fault']} on {f['service']}" for f in faults)
        raise HTTPException(409, f"faults still in place ({names}) - press Rollback first")


@app.post("/experiments")
def start_experiment(req: ExperimentRequest):
    ftype = FAULT_TYPES.get(req.fault_type)
    if not ftype:
        raise HTTPException(400, f"unknown fault_type {req.fault_type!r}; allowed: {sorted(FAULT_TYPES)}")
    exp = {"failure_type": ftype, "target": req.target_service, "hold": req.hold,
           "duration_s": None if req.hold else req.duration_seconds,
           "hypothesis": req.objective,
           "params": {"delay_ms": req.delay_ms} if ftype == "network-delay" and req.delay_ms else {}}
    try:
        validate(exp, cfg)          # reject before scheduling anything
    except ValidationError as e:
        log_event(f"Experiment rejected by safety controller: {e}", "SAFETY")
        raise HTTPException(400, f"rejected by safety controller: {e}") from e
    _require_initial_state()
    _clear_stale_kill_switch()
    jid = _start_job("experiment", lambda: _summary(run_experiment(exp)))
    how_long = "until rollback or kill switch" if req.hold else f"for {req.duration_seconds}s"
    log_event(f"Experiment requested: {ftype} on {req.target_service} {how_long}.", "EXPERIMENT")
    return {"id": jid, "status": "STARTED"}


@app.post("/experiment/stop")
def stop_experiment():
    kill_switch_path(cfg).parent.mkdir(exist_ok=True)
    kill_switch_path(cfg).write_text("stop")
    log_event("Kill switch ON - the agent stops within one monitoring window and leaves the system "
              "frozen in its current state. Press Rollback to restore the initial state.", "SAFETY")
    return {"status": "STOPPING", "kill_switch": True}


def _clear_stale_kill_switch():
    """A new request from the dashboard is an explicit human action; clear a leftover kill switch,
    but never while something is still running."""
    if job["status"] not in ("RUNNING", "PLANNING") and kill_switch_path(cfg).exists():
        kill_switch_path(cfg).unlink()
        log_event("Kill switch cleared for a new request.", "SAFETY")


# ------------------------------------------------------------------ AI agent

agent_view = {"thinking": "", "plan": None}


class AgentRequest(BaseModel):
    count: int = Field(1, ge=1, le=1, description="experiments to run (always 1)")
    services: Optional[list[str]] = None
    execute: bool = True
    hold: bool = Field(True, description="inject only the top pick and keep it until rollback / kill switch")


def _agent_job(req: AgentRequest):
    agent_view["thinking"], agent_view["plan"] = "", None
    job["status"] = "PLANNING"
    log_event(f"AI agent planning with {backend()} model...", "AI")

    def think(chunk):
        agent_view["thinking"] = (agent_view["thinking"] + chunk)[-20000:]

    plan = make_plan(on_thinking=think)
    agent_view["plan"] = plan
    if plan["status"] != "ok":
        log_event(f"AI planner unavailable: {plan.get('error')}. Nothing executed.", "ERROR")
        return {"plan": plan, "runs": []}
    accepted = sum(p["verdict"] == "accepted" for p in plan["proposals"])
    log_event(f"AI plan {plan['plan_id']}: {len(plan['proposals'])} proposals, {accepted} accepted by "
              f"the safety controller.", "AI")
    if not req.execute:
        return {"plan": plan, "runs": []}
    job["status"] = "RUNNING"
    recs = run_plan(plan, top=req.count, services=req.services, hold=req.hold,
                    on_start=lambda p: log_event(f"AI agent running rank {p['rank']}: {p['failure_type']} on "
                                                 f"{p['target']} - {p['reason']}", "AI"))
    return {"plan": plan, "runs": [_summary(r) for r in recs]}


@app.post("/agent/run")
def agent_run(req: AgentRequest):
    if req.execute:
        _require_initial_state()
    _clear_stale_kill_switch()
    return {"id": _start_job("agent", lambda: _agent_job(req)), "status": "STARTED"}


@app.get("/agent")
def agent_state():
    return {"job": {k: job.get(k) for k in ("id", "kind", "status", "error")},
            "thinking": agent_view["thinking"][-4000:], "plan": agent_view["plan"] or latest_plan()}


# ------------------------------------------------------------------ random error injection

RANDOM_DELAYS_MS = [200, 300, 500, 800, 1000, 1500]


class RandomRequest(BaseModel):
    services: Optional[list[str]] = Field(None, description="restrict the random pick to these services")
    hold: bool = Field(True, description="keep the fault active until rollback / kill switch")
    count: int = Field(1, ge=1, le=1, description="always exactly one fault")


@app.post("/experiments/random-errors")
def random_errors(req: RandomRequest = RandomRequest()):
    """Inject exactly one fault, chosen uniformly at random: a random allowed service and a random fault
    type (and a random delay for latency faults). It still goes through validation and the controller."""
    pool = [t for t in cfg["allowed_targets"] if not req.services or t in req.services]
    if not pool:
        raise HTTPException(400, "none of the requested services are on the allowlist")
    _require_initial_state()
    target, ftype = random.choice(pool), random.choice(sorted(CATALOG))
    params = {"delay_ms": random.choice(RANDOM_DELAYS_MS)} if ftype == "network-delay" else {}
    exp = {"failure_type": ftype, "target": target, "params": params, "hold": req.hold,
           "duration_s": None if req.hold else 20, "hypothesis": "random error injection",
           "reason": "chosen at random"}
    try:
        validate(exp, cfg)
    except ValidationError as e:
        raise HTTPException(400, f"rejected by safety controller: {e}") from e
    _clear_stale_kill_switch()
    jid = _start_job("random", lambda: _summary(run_experiment(exp)))
    extra = f" (+{params['delay_ms']} ms)" if params else ""
    log_event(f"Random error chosen: {ftype} on {target}{extra}"
              f"{' - held until rollback or kill switch' if req.hold else ''}.", "EXPERIMENT")
    return {"id": jid, "status": "STARTED", "chosen": {"target": target, "failure_type": ftype, "params": params}}


@app.post("/experiments/random-errors/stop")
def random_errors_stop():
    return stop_experiment()


# ------------------------------------------------------------------ multi-agent squad (3 models, 1 error each)

@app.post("/squad/launch")
def squad_launch():
    """Three agents on three different models each choose and inject one error (different services).
    Errors are held until rollback / kill switch; after squad.OBSERVE_S the analyst model ranks them
    by severity and recommends fixes."""
    _require_initial_state()
    _clear_stale_kill_switch()
    if not ingestor.running:
        ingestion_start()   # live telemetry is needed for the health check and the severity ranking
    jid = _start_job("squad", lambda: squad.run(ingestor, log_event))
    log_event("Squad launched: 3 agents on 3 models will each choose one error.", "AI")
    return {"id": jid, "status": "STARTED", "agents": [{k: a[k] for k in ("id", "model")} for a in squad.AGENTS],
            "note": "error types are assigned to the agents at random for each run"}


@app.post("/squad/pause")
def squad_pause():
    """Pause the LangGraph run at the next step boundary. Its memory is kept in the checkpoint."""
    from . import agent_graph
    if not squad.is_active() or job.get("kind") != "squad" or job["status"] != "RUNNING":
        raise HTTPException(409, "no running squad to pause")
    if squad.STATE.get("paused"):
        raise HTTPException(409, "already paused")
    agent_graph.CONTROL["pause"] = True
    squad.STATE["pause_requested"] = True
    log_event("Pause requested: the LangGraph run stops after the current step and saves its checkpoint.", "AI")
    return {"status": "PAUSE REQUESTED", "note": "takes effect when the current step finishes"}


@app.post("/squad/continue")
def squad_continue():
    """Resume the paused run from its LangGraph checkpoint (same thread, same shared memory)."""
    if not squad.STATE.get("paused") or not squad.is_active():
        raise HTTPException(409, "no paused squad run to continue")
    if kill_switch_path(cfg).exists():
        raise HTTPException(409, "kill switch is on - roll back instead of continuing")
    _start_job("squad", lambda: squad.resume(log_event))
    return {"status": "CONTINUING", "from": squad.STATE.get("graph_trace", [])[-1:]}


@app.get("/squad/graph")
def squad_graph():
    """The LangGraph graph (nodes and edges) and what its checkpointer has saved for the current run."""
    from . import agent_graph
    g = agent_graph.SQUAD_GRAPH.get_graph()
    return {"nodes": [n for n in g.nodes if not n.startswith("__")],
            "edges": [{"from": e.source, "to": e.target, "conditional": e.conditional} for e in g.edges],
            "checkpoint": agent_graph.checkpoint_info() if squad.is_active() else None,
            "paused": bool(squad.STATE.get("paused")), "pause_requested": bool(squad.STATE.get("pause_requested")),
            "mermaid": g.draw_mermaid()}


@app.get("/squad")
def squad_state():
    return squad.public_state()


@app.get("/squad/severity")
def squad_severity():
    """The active run's severity ranking (rank 1 = most severe); null until analysed or when no run is active."""
    return squad.STATE["severity"] if squad.is_active() else None


@app.get("/squad/fixes")
def squad_fixes():
    return squad.STATE["fixes"] if squad.is_active() else {}


FAULT_CAUSE = {"service-down": "service outage (scaled to 0 replicas)", "pod-kill": "crash loop (pods killed repeatedly)",
               "network-delay": "network latency on every call into the service"}
last_rollback = {"result": None}


@app.get("/findings")
def findings():
    """The latest squad's findings for the report: severity-ranked errors with measured impact, suspected
    cause and the analyst's fix. resilience_score = 100 - (0.6 x worst + 0.4 x mean) formula severity,
    so one critical error dominates; null until the severity ranking exists."""
    # Findings belong to the run in progress only; once it is rolled back (or none was launched) they
    # are cleared from the page. Past runs stay available under /squad/history.
    active = squad.is_active()
    sv = squad.STATE["severity"] if active else None
    if not sv:
        return {"squad_id": squad.STATE["id"] if active else None, "status": squad.STATE["status"],
                "active": active, "resilience_score": None, "findings": [],
                "analysis_status": squad.STATE["analysis_status"] if active else None,
                "analysis_error": squad.STATE["analysis_error"] if active else None,
                "ingestion": "RUNNING" if ingestor.running else "STOPPED",
                "previous_squad_id": None if active else squad.STATE["id"],
                "last_rollback": last_rollback["result"]}
    scores = [e["formula_score"] for e in sv["ranked"]]
    score = round(100 - (0.6 * max(scores) + 0.4 * sum(scores) / len(scores)))
    out = []
    for e in sv["ranked"]:
        params = e.get("params") or {}
        out.append({"rank": e["rank"], "severity": e["severity"], "formula_score": e["formula_score"],
                    "target": e["target"], "failure_type": e["failure_type"], "params": params,
                    "agent": e["agent"], "agent_model": e.get("model"), "ranked_by": e.get("ranked_by"),
                    "justification": e["justification"], "impact": e["impact"],
                    "suspected_cause": f"{FAULT_CAUSE.get(e['failure_type'], e['failure_type'])} on {e['target']}"
                                       + (f" (+{params['delay_ms']} ms)" if params.get("delay_ms") else ""),
                    "fix": squad.STATE["fixes"].get(e["agent"])})
    return {"squad_id": squad.STATE["id"], "status": squad.STATE["status"], "active": True,
            "ingestion": "RUNNING" if ingestor.running else "STOPPED",
            "analysis_status": squad.STATE["analysis_status"], "analysis_error": squad.STATE["analysis_error"],
            "analyst_model": sv.get("model") or "formula",
            "ranking_note": sv.get("note"), "resilience_score": score,
            "score_basis": "100 - (0.6 x worst + 0.4 x mean) severity formula score of the injected errors",
            "weights": sv.get("weights"), "findings": out, "last_rollback": last_rollback["result"]}


@app.get("/database")
def database():
    """Where everything is stored, and how many rows each table holds."""
    return {"path": str(db.DB_PATH), "tables": db.table_counts()}


@app.get("/memory")
def shared_memory():
    """The agents' long-term shared memory (LangGraph Store): services tested, runs remembered."""
    from . import memory
    return memory.summary()


@app.get("/squad/history")
def squad_history(limit: int = 20):
    rows = []
    for q in db.squads(limit):
        ranked = (q.get("severity") or {}).get("ranked") or []
        rows.append({"id": q.get("id"), "status": q.get("status"),
                     "errors": [{"agent": a["id"], "model": a["model"], "target": (a.get("pick") or {}).get("target"),
                                 "failure_type": (a.get("pick") or {}).get("failure_type")} for a in q.get("agents", [])],
                     "ranking": [{"rank": e["rank"], "target": e["target"], "severity": e["severity"]} for e in ranked],
                     "analyst_model": (q.get("severity") or {}).get("model")})
    return rows


@app.post("/squad/analyze")
def squad_analyze():
    """Re-run severity ranking + fix recommendations on the latest telemetry."""
    if not squad.is_active() or not any(a.get("applied") for a in squad.STATE["agents"]):
        raise HTTPException(409, "no active squad errors to analyse - launch telemetry and the 3 agents first")
    if squad.STATE["analysis_status"] == "running":
        raise HTTPException(409, "analysis already running")
    threading.Thread(target=squad.analyze, args=(ingestor, log_event), daemon=True).start()
    return {"status": "STARTED"}


# ------------------------------------------------------------------ rollback

class RollbackRequest(BaseModel):
    confirm: bool = False


_rollback_lock = threading.Lock()


@app.post("/rollback")
def rollback(req: RollbackRequest):
    if not req.confirm:
        raise HTTPException(400, "rollback requires confirm=true")
    return _do_rollback("operator")


def _do_rollback(by):
    """Shared by the Rollback button and the emergency guardrail; one rollback at a time."""
    with _rollback_lock:
        return _rollback_locked(by)


def _rollback_locked(by):
    log_event("Rollback requested" + (" by the emergency guardrail" if by == "guardrail" else "")
              + ": stopping the agent and restoring the initial state.", "RECOVERY")
    kill_switch_path(cfg).parent.mkdir(exist_ok=True)
    kill_switch_path(cfg).write_text("rollback")      # stop any running process first (it freezes)
    deadline = time.time() + 90
    while (controller.CURRENT["run"] or job["status"] in ("RUNNING", "PLANNING")) and time.time() < deadline:
        time.sleep(1)
    actions = reset_all(kube, cfg["allowed_targets"], _desired_replicas())
    squad.mark_rolled_back()
    _refresh_faults()
    # Wait for every deployment to be ready again.
    not_ready = []
    while time.time() < deadline:
        not_ready = [t for t in cfg["allowed_targets"] if kube.ready(t) < 1]
        if not not_ready:
            break
        time.sleep(2)
    if job["status"] not in ("RUNNING", "PLANNING"):
        kill_switch_path(cfg).unlink(missing_ok=True)
    restored_at = time.time()
    # Pods being ready is not enough: callers (e.g. the frontend's gRPC clients) can keep failing
    # while they back off before reconnecting. Wait for user-facing health, up to the recovery timeout.
    health = ingestor.health()
    if ingestor.running:
        verify_deadline = restored_at + cfg["recovery_timeout_s"]
        time.sleep(3 * 3)   # at least three fresh telemetry windows
        health = ingestor.health()
        while health.get("healthy") is not True and time.time() < verify_deadline:
            time.sleep(3)
            health = ingestor.health()
    verified = not not_ready and health.get("healthy") is True
    recovery_s = round(time.time() - restored_at, 1) if verified else None
    last_rollback["result"] = {"at": _now_iso(), "recovery_verified": verified, "recovery_time_s": recovery_s,
                               "actions": actions, "by": by}
    log_event(f"Rollback finished: {len(actions)} change(s) reverted; recovery verified = {verified}"
              + (f" ({recovery_s}s after restore)." if verified else "."), "RECOVERY", actions=actions)
    return {"recovery_verified": verified, "recovery_time_s": recovery_s, "actions": actions,
            "not_ready": not_ready, "health": health, "by": by,
            "note": None if ingestor.running else "start ingestion to verify user-facing health"}


# ------------------------------------------------------------------ emergency guardrail (automatic rollback)

GUARD = {"breach_since": None, "last_window": 0, "last_triggered": None}


def _shop_dead(row):
    """True if this telemetry window shows the shop essentially down (thresholds in safety.yaml)."""
    if not row["n"]:
        return bool(row.get("stalled"))          # nothing finished and requests are hanging
    browse = row.get("browse_error_rate")
    return row["error_rate"] >= cfg["guardrail_error_rate"] or (
        browse is not None and browse >= cfg["guardrail_browse_error_rate"])


def _guardrail():
    """Every telemetry window: if faults are held and the shop has been essentially dead for
    guardrail_duration_s, roll back automatically. Below that bar, faults stay until an operator acts."""
    while True:
        time.sleep(1)
        try:
            rows = list(ingestor.metrics)
            if not rows or rows[-1]["t"] == GUARD["last_window"]:
                continue
            row = rows[-1]
            GUARD["last_window"] = row["t"]
            if not ingestor.running or not active_faults() or _rollback_lock.locked() or not _shop_dead(row):
                GUARD["breach_since"] = None
                continue
            GUARD["breach_since"] = GUARD["breach_since"] or row["t"] - ingestor_window_s()
            dead_for = row["t"] - GUARD["breach_since"]
            if dead_for >= cfg["guardrail_duration_s"]:
                reason = (f"shop essentially down for {dead_for:.0f} s (error rate {pct_text(row['error_rate'])}, "
                          f"browse {pct_text(row.get('browse_error_rate'))}; limit {pct_text(cfg['guardrail_error_rate'])} "
                          f"for {cfg['guardrail_duration_s']} s)")
                GUARD.update(breach_since=None, last_triggered={"at": _now_iso(), "reason": reason})
                log_event(f"GUARDRAIL: automatic rollback - {reason}.", "SAFETY")
                _do_rollback("guardrail")
        except Exception as e:  # noqa: BLE001 - the guardrail must keep running
            log_event(f"Guardrail check failed: {e}", "ERROR")


def ingestor_window_s():
    from .telemetry import WINDOW_S
    return WINDOW_S


def pct_text(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


threading.Thread(target=_guardrail, daemon=True).start()
