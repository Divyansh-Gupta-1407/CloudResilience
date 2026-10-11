"""Chaos squad: three agents, each driven by a different local model, each injects ONE error.

  Agent A  qwen3:4b        Agent B  ministral-3:3b        Agent C  llama3.2:3b

Every launch is randomised so runs differ:
  - the three error types (outage, crash loop, network latency) are shuffled across the agents;
  - each agent chooses only from a random shortlist of services (avoiding the previous run's targets
    when enough others remain);
  - every model call gets a random sampling seed.
Each model only chooses a target (and a delay for latency) from that schema-constrained shortlist;
every pick goes through controller.validate(). The agents take turns (the laptop GPU fits one model
at a time) and must pick different services. The three faults are then held together until rollback
or the kill switch. After OBSERVE_S of live telemetry the analyst model ranks them by severity
(severity.py) and recommends fixes (fixes.py).
"""
import glob
import json
import os
import random
import threading
import time
from pathlib import Path

from . import controller, fixes, severity
from .analyst import status as analyst_status
from .controller import OUT, ValidationError, kill_switch_path, load_safety, validate
from .discovery import DEFAULT_MANIFEST, discover
from .faults import CATALOG, Kube
from .llm import LLMError, chat_json, installed_models

AGENTS = [
    {"id": "A", "model": os.environ.get("CHAOS_AGENT_A_MODEL", "qwen3:4b"), "think": True},
    {"id": "B", "model": os.environ.get("CHAOS_AGENT_B_MODEL", "ministral-3:3b"), "think": False},
    {"id": "C", "model": os.environ.get("CHAOS_AGENT_C_MODEL", "llama3.2:3b"), "think": False},
]
ROLES = {"service-down": "Outage agent", "pod-kill": "Crash agent", "network-delay": "Latency agent"}
EXCLUDED = {"frontend"}   # the entry point: breaking it takes the whole shop down, nothing to compare
# Services that the shop cannot tell apart from the outside (cartservice is redis-cart's only caller,
# and its errors name cartservice either way), so two agents may not split such a pair.
LINKED = {"cartservice": {"redis-cart"}, "redis-cart": {"cartservice"}}
SHORTLIST = 4             # services each agent may choose from (random each run)
SETTLE_S = 45             # longest wait for a healthy shop before injecting
OBSERVE_S = 30            # minimum telemetry collected before the analyst ranks the errors and suggests fixes
MIN_REQUESTS = 150        # ... and at least this many shopper requests (slow faults mean fewer requests per second)
OBSERVE_MAX_S = 90        # ... but never wait longer than this
DELAY_CHOICES = (200, 3000)
ACTIVE = ("PLANNING", "INJECTING", "HOLDING", "FROZEN")   # a run whose results belong on screen

STATE = {"id": None, "status": "IDLE", "agents": [], "started": None, "injected_at": None,
         "severity": None, "fixes": {}, "analysis_status": None, "analysis_error": None, "note": None,
         "last_id": None}
_lock = threading.Lock()


def _fresh_agents():
    """New roster for one launch: the error types are shuffled across the three models."""
    roles = random.sample(list(ROLES), len(AGENTS))
    return [{**a, "fault": f, "name": ROLES[f], "status": "waiting", "thinking": "", "pick": None,
             "error": None, "fallback": False, "applied": False} for a, f in zip(AGENTS, roles)]


def is_active():
    return STATE["status"] in ACTIVE


def public_state():
    with _lock:
        agents = [{k: v for k, v in a.items() if k != "thinking"} | {"thinking": a["thinking"][-1500:]}
                  for a in STATE["agents"]]
        return {**{k: v for k, v in STATE.items() if k != "agents"}, "agents": agents, "active": is_active(),
                "analyst": analyst_status(), "observe_s": OBSERVE_S, "min_requests": MIN_REQUESTS,
                "observe_max_s": OBSERVE_MAX_S}


def _save():
    (OUT / "squads").mkdir(parents=True, exist_ok=True)
    (OUT / "squads" / f"{STATE['id']}.json").write_text(json.dumps(public_state(), indent=2, default=str))


def _previous_targets():
    files = sorted(glob.glob(str(OUT / "squads" / "*.json")))
    if not files:
        return set()
    try:
        q = json.loads(Path(files[-1]).read_text())
        return {(a.get("pick") or {}).get("target") for a in q.get("agents", [])} - {None}
    except (OSError, ValueError):
        return set()

# ------------------------------------------------------------------ choosing

def _pick_schema(targets, fault):
    props = {"target": {"type": "string", "enum": targets},
             "hypothesis": {"type": "string", "description": "What you expect shoppers to see"},
             "reason": {"type": "string", "description": "Why this target, in one sentence"}}
    if fault == "network-delay":
        props["delay_ms"] = {"type": "integer", "minimum": DELAY_CHOICES[0], "maximum": DELAY_CHOICES[1]}
    return {"type": "object", "properties": props, "required": list(props)}


def _prompts(agent, targets, taken):
    dep = discover(DEFAULT_MANIFEST)
    services = {n: {"role": s.get("role"), "critical": bool(s.get("critical")), "called_by": s.get("callers")}
                for n, s in dep["services"].items() if n in targets}
    fault_doc = " ".join((CATALOG[agent["fault"]].__doc__ or "").split())
    system = (f"You are {agent['name']}, one of three chaos-engineering agents testing an online shop "
              f"(Google's Online Boutique, Kubernetes microservices) inside a sandbox. Your only failure "
              f"type is: {agent['fault']} - {fault_doc} Pick ONE target service where this failure is the "
              f"most informative resilience test. Answer with JSON only.")
    user = json.dumps({"available_targets": services, "user_journeys": dep["journeys"],
                       "already_taken_by_other_agents": taken,
                       **({"delay_ms_range": list(DELAY_CHOICES)} if agent["fault"] == "network-delay" else {})},
                      indent=1)
    return system, user


def _choose(agent, cfg, taken, avoid):
    blocked = set(taken) | {l for t in taken for l in LINKED.get(t, ())}
    allowed = [t for t in cfg["allowed_targets"] if t not in EXCLUDED and t not in blocked]
    fresh = [t for t in allowed if t not in avoid]
    pool = fresh if len(fresh) >= SHORTLIST else allowed      # prefer services the last run did not hit
    targets = random.sample(pool, min(SHORTLIST, len(pool)))
    agent["shortlist"] = targets
    system, user = _prompts(agent, targets, taken)

    def think(chunk):
        with _lock:
            agent["thinking"] = (agent["thinking"] + chunk)[-6000:]

    last_err = None
    for attempt in (1, 2):
        try:
            data, _ = chat_json(agent["model"], system, user, _pick_schema(targets, agent["fault"]),
                                think=agent["think"], on_thinking=think, temperature=0.9, keep_alive=0,
                                seed=random.randint(1, 2**31 - 1))
            exp = {"failure_type": agent["fault"], "target": data.get("target"), "hold": True,
                   "params": {"delay_ms": data.get("delay_ms")} if agent["fault"] == "network-delay" else {},
                   "hypothesis": data.get("hypothesis", ""), "reason": data.get("reason", "")}
            if exp["target"] not in targets:
                raise ValidationError(f"target {exp['target']!r} is not available")
            return validate(exp, cfg), False
        except (LLMError, ValidationError) as e:
            last_err = str(e)
            think(f"\n[attempt {attempt} rejected: {last_err[:200]}]\n")
    # The model could not produce a valid pick: keep the demo going with a random valid one, flagged.
    target = random.choice(targets)
    params = {"delay_ms": random.choice([300, 500, 800, 1000])} if agent["fault"] == "network-delay" else {}
    agent["error"] = f"model pick unusable ({last_err[:160]}); random fallback used"
    return validate({"failure_type": agent["fault"], "target": target, "hold": True, "params": params,
                     "hypothesis": "fallback pick", "reason": "random fallback"}, cfg), True


# ------------------------------------------------------------------ the run

def run(ingestor, log):
    """Background job: choose (3 models, one after another) -> health check -> inject all -> hold."""
    cfg = load_safety()
    kube = Kube(cfg["cluster_context"], cfg["namespace"])
    stop = kill_switch_path(cfg)
    avoid = _previous_targets()
    with _lock:
        STATE.update(last_id=STATE["id"], id=time.strftime("%Y%m%d-%H%M%S"), status="PLANNING",
                     agents=_fresh_agents(), started=time.time(), injected_at=None, severity=None, fixes={},
                     analysis_status=None, analysis_error=None, note=None)
    log("Squad roles this run: " + ", ".join(f"{a['id']} ({a['model']}) = {a['name']}" for a in STATE["agents"]), "AI")
    have = installed_models()
    missing = [a["model"] for a in AGENTS if have and a["model"] not in have and f"{a['model']}:latest" not in have]
    if missing:
        STATE.update(status="FAILED", note=f"models not installed: {', '.join(missing)} (ollama pull ...)")
        log(STATE["note"], "ERROR")
        _save()
        return public_state()

    taken = []
    for agent in STATE["agents"]:
        if stop.exists():
            STATE.update(status="STOPPED", note="kill switch before injection - nothing was changed")
            _save()
            return public_state()
        agent["status"] = "thinking"
        log(f"Agent {agent['id']} ({agent['model']}) choosing a target for {agent['fault']}...", "AI")
        exp, fallback = _choose(agent, cfg, taken, avoid)
        agent.update(pick=exp, fallback=fallback, status="chosen")
        taken.append(exp["target"])
        extra = f" +{exp['params']['delay_ms']} ms" if exp["params"].get("delay_ms") else ""
        log(f"Agent {agent['id']} ({agent['model']}) picked {exp['failure_type']} on {exp['target']}{extra}"
            f"{' (random fallback)' if fallback else ''}: {exp['reason'][:160]}", "AI")

    # Steady-state check on live telemetry: never inject into an already-unhealthy shop.
    if not ingestor.running:
        ingestor.start()
        time.sleep(12)
    # The models just ran on the same laptop, which briefly slows the shop's responses; give it up to
    # SETTLE_S to show healthy windows again instead of judging a single snapshot.
    STATE["note"] = "checking the shop is healthy before injecting"
    deadline = time.time() + SETTLE_S
    health = ingestor.health()
    while health.get("healthy") is not True and time.time() < deadline and not stop.exists():
        time.sleep(3)
        health = ingestor.health()
    STATE["note"] = None
    if health.get("healthy") is not True or stop.exists():
        why = "kill switch" if stop.exists() else f"shop not healthy before injection: {health}"
        STATE.update(status="REFUSED", note=why)
        log(f"Squad refused to inject: {why}", "SAFETY")
        _save()
        return public_state()

    STATE["status"] = "INJECTING"
    faults = []
    for agent in STATE["agents"]:
        exp = agent["pick"]
        f = CATALOG[exp["failure_type"]](kube, exp["target"], exp["params"])
        try:
            agent["apply_result"] = f.apply()
            agent.update(applied=True, status="holding", applied_at=time.time())
            faults.append((agent, f))
        except Exception as e:  # noqa: BLE001 - one failed injection must not stop the others
            agent.update(status="failed", error=f"inject failed: {e}")
    controller.CURRENT["squad"] = [{"agent": a["id"], "target": a["pick"]["target"],
                                    "failure_type": a["pick"]["failure_type"]} for a, _ in faults]
    STATE.update(status="HOLDING", injected_at=time.time(), measured_requests=0)
    log(f"Squad injected {len(faults)} error(s): " + "; ".join(
        f"{a['id']}: {a['pick']['failure_type']} on {a['pick']['target']}" for a, _ in faults)
        + " - held until rollback or kill switch.", "EXPERIMENT")
    _save()

    analysis_started = False
    try:
        while True:
            for _, f in faults:
                if hasattr(f, "sustain"):
                    f.sustain()
            if stop.exists():
                for a, f in faults:
                    if hasattr(f, "freeze"):
                        try:
                            f.freeze()
                        except Exception as e:  # noqa: BLE001
                            a["error"] = f"freeze failed: {e}"
                    a["status"] = "frozen"
                STATE["status"] = "FROZEN"
                log("Kill switch: squad stopped, all errors left in place until rollback.", "SAFETY")
                break
            elapsed = time.time() - STATE["injected_at"]
            measured = sum(m["n"] for m in list(ingestor.metrics) if m["t"] >= STATE["injected_at"])
            STATE["measured_requests"] = measured
            if not analysis_started and elapsed >= OBSERVE_S and (measured >= MIN_REQUESTS or elapsed >= OBSERVE_MAX_S):
                analysis_started = True
                threading.Thread(target=analyze, args=(ingestor, log), daemon=True).start()
            if elapsed >= cfg["hold_max_duration_s"]:
                for a, f in faults:
                    f.cleanup()
                    a["status"] = "removed (backstop)"
                STATE["status"] = "DONE"
                log("Squad hold backstop reached: errors removed automatically.", "RECOVERY")
                break
            time.sleep(1)
    finally:
        controller.CURRENT["squad"] = None
        _save()
    return public_state()


def errors_for_analysis(ingestor):
    until = time.time()
    out = []
    for a in STATE["agents"]:
        if not a.get("applied"):
            continue
        out.append({"agent": a["id"], "agent_name": a["name"], "model": a["model"], "target": a["pick"]["target"],
                    "failure_type": a["pick"]["failure_type"], "params": a["pick"]["params"],
                    "hypothesis": a["pick"]["hypothesis"],
                    "impact": severity.measure(ingestor, a["pick"]["target"], a["applied_at"], until)})
    return out


def analyze(ingestor, log):
    """Severity ranking, then fixes (in rank order). Safe to call again to refresh."""
    if STATE["analysis_status"] == "running" or not is_active():
        return
    run_id = STATE["id"]
    errors = errors_for_analysis(ingestor)
    if not errors:
        return
    if not any(e["impact"]["requests_total"] for e in errors):
        STATE.update(analysis_status="failed",
                     analysis_error="no telemetry was recorded while the errors were active - start ingestion")
        log("Analysis skipped: no telemetry recorded during the errors (is ingestion running?)", "ERROR")
        return
    STATE.update(analysis_status="running", analysis_error=None, fixes={})
    try:
        log("Analyst ranking the errors by severity...", "AI")
        ranking = severity.rank_by_severity(errors)
        if STATE["id"] != run_id:      # a new run started meanwhile: drop these stale results
            return
        STATE["severity"] = ranking
        order = [e["agent"] for e in STATE["severity"]["ranked"]]
        log("Severity ranking: " + ", ".join(f"#{e['rank']} {e['target']} ({e['severity']})"
                                             for e in STATE["severity"]["ranked"]), "AI")
        ranked_errors = sorted(errors, key=lambda e: order.index(e["agent"]))
        fixes.recommend_all(ranked_errors, on_done=lambda aid, fx: STATE["id"] == run_id and STATE["fixes"].__setitem__(aid, fx))
        if STATE["id"] != run_id:
            return
        STATE["analysis_status"] = "done"
        log("Analyst finished fix recommendations for all errors.", "AI")
    except Exception as e:  # noqa: BLE001
        STATE.update(analysis_status="failed", analysis_error=str(e)[:300])
        log(f"Analysis failed: {e}", "ERROR")
    finally:
        _save()


def mark_rolled_back():
    if STATE["status"] in ("HOLDING", "FROZEN", "INJECTING"):
        STATE["status"] = "ROLLED BACK"
        for a in STATE["agents"]:
            if a.get("applied"):
                a["status"] = "rolled back"
        _save()
