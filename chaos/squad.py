"""Chaos squad: three agents, each driven by a different local model, each injects ONE error.
The run is a LangGraph graph (agent_graph.py) and the agents share memory: the team board for this run
(graph State) and the results of all past runs (LangGraph Store, memory.py).

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
import json
import os
import random
import threading
import time

from . import controller, db, fixes, severity
from .analyst import status as analyst_status
from .controller import ValidationError, kill_switch_path, load_safety, validate
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
ACTIVE = ("PLANNING", "INJECTING", "HOLDING", "FROZEN")
GRAPH_NODES = ["recall_memory", "agent_A", "agent_B", "agent_C", "safety_gate", "inject", "observe", "rank",
               "recommend", "remember"]   # a run whose results belong on screen

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
                "graph_nodes": GRAPH_NODES,
                "observe_max_s": OBSERVE_MAX_S}


def _save():
    db.save_squad(public_state())


def _previous_targets():
    last = db.squads(1)
    return {(a.get("pick") or {}).get("target") for a in (last[0].get("agents", []) if last else [])} - {None}
# ------------------------------------------------------------------ choosing (one agent's decision)

def _pick_schema(targets, fault):
    props = {"target": {"type": "string", "enum": targets},
             "hypothesis": {"type": "string", "description": "What you expect shoppers to see"},
             "reason": {"type": "string", "description": "Why this target, in one or two sentences, referring to the team board and memory"},
             "message_to_team": {"type": "string", "description": "One short note for the other agents and the analyst"}}
    if fault == "network-delay":
        props["delay_ms"] = {"type": "integer", "minimum": DELAY_CHOICES[0], "maximum": DELAY_CHOICES[1]}
    return {"type": "object", "properties": props, "required": list(props)}


def _prompts(agent, targets, taken, board, recall, recent_runs):
    dep = discover(DEFAULT_MANIFEST)
    services = {n: {"role": s.get("role"), "critical": bool(s.get("critical")), "called_by": s.get("callers"),
                    "memory": recall.get(n, {"times_tested": 0})}
                for n, s in dep["services"].items() if n in targets}
    fault_doc = " ".join((CATALOG[agent["fault"]].__doc__ or "").split())
    system = (f"You are {agent['name']} ({agent['id']}), one of three chaos-engineering agents working as a team on "
              f"an online shop (Google's Online Boutique, Kubernetes microservices) inside a sandbox. Your only "
              f"failure type is: {agent['fault']} - {fault_doc}\n"
              "You share memory with your team:\n"
              "- team_board: what your teammates already chose this run and their messages to you. Complement them: "
              "cover a different part of the shop (a different user journey or tier) so the run tests more of it.\n"
              "- memory: results of past runs for each candidate service (times tested, faults tried, worst score). "
              "Prefer services or failure types that were never tested, or that broke badly before and deserve a re-test.\n"
              "Pick ONE target from available_targets, explain why using the board and memory, and leave a short "
              "message for the team. Answer with JSON only.")
    user = json.dumps({"available_targets": services, "user_journeys": dep["journeys"],
                       "team_board": [{k: b[k] for k in ("agent", "role", "target", "reason", "message")} for b in board],
                       "already_taken_by_teammates": taken, "recent_runs": recent_runs,
                       **({"delay_ms_range": list(DELAY_CHOICES)} if agent["fault"] == "network-delay" else {})},
                      indent=1)
    return system, user


def _choose(agent, cfg, taken, avoid, board=(), recall=None, recent_runs=()):
    """One agent's decision. Returns (validated experiment, used_fallback, message_to_team, memory it saw)."""
    recall = recall or {}
    blocked = set(taken) | {l for t in taken for l in LINKED.get(t, ())}
    allowed = [t for t in cfg["allowed_targets"] if t not in EXCLUDED and t not in blocked]
    fresh = [t for t in allowed if t not in avoid]
    pool = fresh if len(fresh) >= SHORTLIST else allowed      # prefer services the last run did not hit
    targets = random.sample(pool, min(SHORTLIST, len(pool)))
    agent["shortlist"] = targets
    recalled = {t: recall.get(t, {"times_tested": 0}) for t in targets}
    system, user = _prompts(agent, targets, taken, list(board), recall, list(recent_runs))

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
            return validate(exp, cfg), False, str(data.get("message_to_team", ""))[:300], recalled
        except (LLMError, ValidationError) as e:
            last_err = str(e)
            think(f"\n[attempt {attempt} rejected: {last_err[:200]}]\n")
    # The model could not produce a valid pick: keep the demo going with a random valid one, flagged.
    target = random.choice(targets)
    params = {"delay_ms": random.choice([300, 500, 800, 1000])} if agent["fault"] == "network-delay" else {}
    agent["error"] = f"model pick unusable ({last_err[:160]}); random fallback used"
    return validate({"failure_type": agent["fault"], "target": target, "hold": True, "params": params,
                     "hypothesis": "fallback pick", "reason": "random fallback"}, cfg), True, "", recalled


# ------------------------------------------------------------------ the run (a LangGraph graph, see agent_graph.py)

def run(ingestor, log):
    """Background job: the agent graph chooses (3 models with shared memory), checks health, injects and
    analyses; the hold loop keeps the errors in place until rollback, kill switch or the backstop."""
    from . import agent_graph
    cfg = load_safety()
    kube = Kube(cfg["cluster_context"], cfg["namespace"])
    stop = kill_switch_path(cfg)
    with _lock:
        STATE.update(last_id=STATE["id"], id=time.strftime("%Y%m%d-%H%M%S"), status="PLANNING",
                     agents=_fresh_agents(), started=time.time(), injected_at=None, severity=None, fixes={},
                     analysis_status=None, analysis_error=None, note=None, board=[], graph_trace=[], paused=False,
                     pause_requested=False,
                     memory_recall=None)
    log("Squad roles this run: " + ", ".join(f"{a['id']} ({a['model']}) = {a['name']}" for a in STATE["agents"]), "AI")
    have = installed_models()
    missing = [a["model"] for a in AGENTS if have and a["model"] not in have and f"{a['model']}:latest" not in have]
    if missing:
        STATE.update(status="FAILED", note=f"models not installed: {', '.join(missing)} (ollama pull ...)")
        log(STATE["note"], "ERROR")
        _save()
        return public_state()

    return _finish(agent_graph.run_squad(ingestor, log, cfg, kube, stop))


def resume(log):
    """Continue a paused LangGraph run from its checkpoint (the Continue button)."""
    from . import agent_graph
    return _finish(agent_graph.resume_squad(log))


def _finish(final):
    from . import agent_graph
    try:
        if final.get("paused"):
            return public_state()          # the checkpoint holds the run; held errors stay in place
        if final.get("status") in ("STOPPED", "REFUSED") and not agent_graph.RUNTIME.get("hold"):
            STATE.update(status=final["status"], note=final.get("note"))
        hold = agent_graph.RUNTIME.get("hold")
        if hold:
            hold.join()           # errors stay in place until rollback, kill switch or backstop
    finally:
        if not STATE.get("paused"):
            controller.CURRENT["squad"] = None
        _save()
    return public_state()


def hold_loop(faults, cfg, log):
    """Keeps the injected errors in place: crash loops alive, kill switch -> freeze, 10-minute backstop."""
    stop = kill_switch_path(cfg)
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
            return
        if time.time() - STATE["injected_at"] >= cfg["hold_max_duration_s"]:
            for a, f in faults:
                f.cleanup()
                a["status"] = "removed (backstop)"
            STATE["status"] = "DONE"
            log("Squad hold backstop reached: errors removed automatically.", "RECOVERY")
            return
        time.sleep(1)

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
    """Re-run severity ranking, fixes and the memory update through the analysis graph."""
    if STATE["analysis_status"] == "running" or not is_active():
        return
    from . import agent_graph
    agent_graph.run_analysis(ingestor, log)

def mark_rolled_back():
    if STATE.get("paused") and STATE["status"] == "PLANNING":
        STATE["status"] = "STOPPED"
    STATE.update(paused=False, pause_requested=False)
    if STATE["status"] in ("HOLDING", "FROZEN", "INJECTING"):
        STATE["status"] = "ROLLED BACK"
        for a in STATE["agents"]:
            if a.get("applied"):
                a["status"] = "rolled back"
        _save()
