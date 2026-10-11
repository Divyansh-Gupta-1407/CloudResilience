"""The chaos squad as a LangGraph multi-agent graph with shared memory.

    recall_memory -> agent_A -> agent_B -> agent_C -> safety_gate -> inject -> observe -> rank -> recommend -> remember

Shared memory, two kinds:
  * Short-term (this run): the graph State. Every agent reads the team board (what its teammates chose,
    why, and their message to the team) and posts its own decision; the analyst reads all of it.
  * Long-term (across runs): the LangGraph Store in memory.py. recall_memory loads what was tested
    before and how badly it broke; the agents and the analyst use it; remember writes this run back.

Safety is unchanged: models only return JSON that is validated before anything happens; injection,
holding, kill switch, rollback and guardrail are the same deterministic code as before. While the
analysis nodes run, a separate hold thread keeps crash loops going and reacts to the kill switch
within a second.
"""
import operator
import threading
import time
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_store
from langgraph.graph import END, START, StateGraph

from . import controller, fixes, memory, severity
from . import squad as sq
from .faults import CATALOG

RUNTIME = {}   # live objects for the current run (telemetry ingestor, logger, fault objects) - not graph state


class SquadState(TypedDict, total=False):
    run_id: str
    recall: dict                                   # long-term memory view for every candidate service
    recent_runs: list                              # last runs remembered (targets, resilience score)
    avoid: list                                    # previous run's targets
    board: Annotated[list, operator.add]           # the team board: one entry per agent decision
    trace: Annotated[list, operator.add]           # nodes executed, for the UI
    status: str                                    # PLANNING | STOPPED | REFUSED | HOLDING | ANALYSED
    note: str
    ranking: dict
    fix_count: int


def _log(msg, cat="AI"):
    RUNTIME["log"](msg, cat)


def _stopped():
    return RUNTIME["stop"].exists()


# ------------------------------------------------------------------ nodes

def recall_memory(state: SquadState):
    store = get_store()                            # the same Store memory.py persists
    cfg = RUNTIME["cfg"]
    candidates = [t for t in cfg["allowed_targets"] if t not in sq.EXCLUDED]
    runs = sorted((i.value | {"run": i.key} for i in store.search(memory.RUNS, limit=1000)), key=lambda r: r["at"])
    recall = memory.recall(candidates)
    tested = sum(1 for v in recall.values() if v["times_tested"])
    _log(f"Shared memory recalled: {len(runs)} past run(s), {tested} of {len(candidates)} services tested before.")
    sq.STATE["memory_recall"] = {"runs": len(runs), "services_tested": tested, "services_total": len(candidates)}
    return {"recall": recall, "recent_runs": runs[-3:], "avoid": sorted(sq._previous_targets()),
            "status": "PLANNING", "trace": ["recall_memory"]}


def make_agent(idx):
    def agent_node(state: SquadState):
        agent = sq.STATE["agents"][idx]
        if _stopped():
            return {"status": "STOPPED", "note": "kill switch before injection - nothing was changed",
                    "trace": [f"agent_{agent['id']} (skipped)"]}
        agent["status"] = "thinking"
        _log(f"Agent {agent['id']} ({agent['model']}) reading the team board and memory, choosing a "
             f"{agent['name'].lower().replace(' agent', '')} target...")
        taken = [b["target"] for b in state.get("board", [])]
        exp, fallback, message, recalled = sq._choose(agent, RUNTIME["cfg"], taken, set(state.get("avoid", [])),
                                                       board=state.get("board", []), recall=state.get("recall", {}),
                                                       recent_runs=state.get("recent_runs", []))
        agent.update(pick=exp, fallback=fallback, status="chosen", message=message, recalled=recalled)
        entry = {"agent": agent["id"], "model": agent["model"], "role": agent["name"], "fault": exp["failure_type"],
                 "target": exp["target"], "delay_ms": exp["params"].get("delay_ms"), "reason": exp["reason"],
                 "hypothesis": exp["hypothesis"], "message": message, "fallback": fallback}
        sq.STATE["board"] = sq.STATE.get("board", []) + [entry]
        extra = f" +{exp['params']['delay_ms']} ms" if exp["params"].get("delay_ms") else ""
        _log(f"Agent {agent['id']} ({agent['model']}) picked {exp['failure_type']} on {exp['target']}{extra}"
             f"{' (random fallback)' if fallback else ''}: {exp['reason'][:140]}"
             + (f" | to team: {message[:120]}" if message else ""))
        return {"board": [entry], "trace": [f"agent_{agent['id']}"]}
    return agent_node


def safety_gate(state: SquadState):
    ingestor, stop = RUNTIME["ingestor"], RUNTIME["stop"]
    if not ingestor.running:
        ingestor.start()
        time.sleep(12)
    sq.STATE["note"] = "checking the shop is healthy before injecting"
    deadline = time.time() + sq.SETTLE_S
    health = ingestor.health()
    while health.get("healthy") is not True and time.time() < deadline and not stop.exists():
        time.sleep(3)
        health = ingestor.health()
    sq.STATE["note"] = None
    if health.get("healthy") is not True or stop.exists():
        why = "kill switch" if stop.exists() else f"shop not healthy before injection: {health}"
        _log(f"Squad refused to inject: {why}", "SAFETY")
        return {"status": "REFUSED", "note": why, "trace": ["safety_gate (refused)"]}
    return {"trace": ["safety_gate"]}


def inject(state: SquadState):
    kube, faults = RUNTIME["kube"], []
    sq.STATE["status"] = "INJECTING"
    for agent in sq.STATE["agents"]:
        exp = agent["pick"]
        f = CATALOG[exp["failure_type"]](kube, exp["target"], exp["params"])
        try:
            agent["apply_result"] = f.apply()
            agent.update(applied=True, status="holding", applied_at=time.time())
            faults.append((agent, f))
        except Exception as e:  # noqa: BLE001 - one failed injection must not stop the others
            agent.update(status="failed", error=f"inject failed: {e}")
    RUNTIME["faults"] = faults
    controller.CURRENT["squad"] = [{"agent": a["id"], "target": a["pick"]["target"],
                                    "failure_type": a["pick"]["failure_type"]} for a, _ in faults]
    sq.STATE.update(status="HOLDING", injected_at=time.time(), measured_requests=0)
    _log(f"Squad injected {len(faults)} error(s): " + "; ".join(
        f"{a['id']}: {a['pick']['failure_type']} on {a['pick']['target']}" for a, _ in faults)
        + " - held until rollback or kill switch.", "EXPERIMENT")
    sq._save()
    # The hold thread owns the faults from now on (crash loops, kill switch, backstop), so the kill
    # switch keeps working while the analyst nodes are busy.
    RUNTIME["hold"] = threading.Thread(target=sq.hold_loop, args=(faults, RUNTIME["cfg"], RUNTIME["log"]), daemon=True)
    RUNTIME["hold"].start()
    return {"status": "HOLDING", "trace": ["inject"]}


def observe(state: SquadState):
    ingestor = RUNTIME["ingestor"]
    while True:
        if sq.STATE["status"] != "HOLDING":
            return {"status": "STOPPED", "trace": ["observe (stopped)"]}
        elapsed = time.time() - sq.STATE["injected_at"]
        measured = sum(m["n"] for m in list(ingestor.metrics) if m["t"] >= sq.STATE["injected_at"])
        sq.STATE["measured_requests"] = measured
        if elapsed >= sq.OBSERVE_S and (measured >= sq.MIN_REQUESTS or elapsed >= sq.OBSERVE_MAX_S):
            return {"trace": [f"observe ({measured} requests, {elapsed:.0f} s)"]}
        time.sleep(1)


def rank(state: SquadState):
    errors = sq.errors_for_analysis(RUNTIME["ingestor"])
    if not errors or not any(e["impact"]["requests_total"] for e in errors):
        sq.STATE.update(analysis_status="failed",
                        analysis_error="no telemetry was recorded while the errors were active - start ingestion")
        _log("Analysis skipped: no telemetry recorded during the errors (is ingestion running?)", "ERROR")
        return {"status": "STOPPED", "trace": ["rank (no telemetry)"]}
    sq.STATE.update(analysis_status="running", analysis_error=None, fixes={})
    _log("Analyst ranking the errors by severity, using the team board and past results...")
    for e in errors:
        e["history"] = memory.history_for(e["target"], e["failure_type"])
        e["team_note"] = next((b for b in state.get("board", []) if b["agent"] == e["agent"]), None)
    RUNTIME["errors"] = errors
    try:
        ranking = severity.rank_by_severity(errors)
    except Exception as ex:  # noqa: BLE001
        sq.STATE.update(analysis_status="failed", analysis_error=str(ex)[:300])
        _log(f"Analysis failed: {ex}", "ERROR")
        return {"status": "STOPPED", "trace": ["rank (failed)"]}
    sq.STATE["severity"] = ranking
    _log("Severity ranking: " + ", ".join(f"#{e['rank']} {e['target']} ({e['severity']})" for e in ranking["ranked"]))
    return {"ranking": {"order": [e["agent"] for e in ranking["ranked"]]}, "trace": ["rank"]}


def recommend(state: SquadState):
    order = state["ranking"]["order"]
    ranked = sorted(RUNTIME["errors"], key=lambda e: order.index(e["agent"]))
    run_id = sq.STATE["id"]
    out = fixes.recommend_all(ranked, on_done=lambda aid, fx: sq.STATE["id"] == run_id
                              and sq.STATE["fixes"].__setitem__(aid, fx))
    return {"fix_count": sum(1 for f in out.values() if "error" not in f), "trace": ["recommend"]}


def remember(state: SquadState):
    ranked = (sq.STATE.get("severity") or {}).get("ranked") or []
    if ranked:
        scores = [f["formula_score"] for f in ranked]
        resilience = round(100 - (0.6 * max(scores) + 0.4 * sum(scores) / len(scores)))
        memory.remember_run(sq.STATE["id"], ranked, resilience)
        _log(f"Shared memory updated: run {sq.STATE['id']} (resilience {resilience}) stored for future agents.")
    sq.STATE["analysis_status"] = "done"
    sq._save()
    return {"status": "ANALYSED", "trace": ["remember"]}


# ------------------------------------------------------------------ routing

def _go_on(next_node):
    return lambda s: END if s.get("status") in ("STOPPED", "REFUSED") else next_node


def _build_squad_graph():
    g = StateGraph(SquadState)
    g.add_node("recall_memory", recall_memory)
    for i, name in enumerate(("agent_A", "agent_B", "agent_C")):
        g.add_node(name, make_agent(i))
    for name, fn in (("safety_gate", safety_gate), ("inject", inject), ("observe", observe),
                     ("rank", rank), ("recommend", recommend), ("remember", remember)):
        g.add_node(name, fn)
    g.add_edge(START, "recall_memory")
    g.add_edge("recall_memory", "agent_A")
    chain = ["agent_A", "agent_B", "agent_C", "safety_gate", "inject", "observe", "rank", "recommend", "remember"]
    for a, b in zip(chain, chain[1:]):
        if a in ("inject", "recommend"):
            g.add_edge(a, b)
        else:
            g.add_conditional_edges(a, _go_on(b), [b, END])
    g.add_edge("remember", END)
    return g.compile(checkpointer=InMemorySaver(), store=memory.STORE)


def _build_analysis_graph():
    g = StateGraph(SquadState)
    for name, fn in (("rank", rank), ("recommend", recommend), ("remember", remember)):
        g.add_node(name, fn)
    g.add_edge(START, "rank")
    g.add_conditional_edges("rank", _go_on("recommend"), ["recommend", END])
    g.add_edge("recommend", "remember")
    g.add_edge("remember", END)
    return g.compile(store=memory.STORE)


SQUAD_GRAPH = _build_squad_graph()
ANALYSIS_GRAPH = _build_analysis_graph()
NODES = ["recall_memory", "agent_A", "agent_B", "agent_C", "safety_gate", "inject", "observe", "rank",
         "recommend", "remember"]


CONTROL = {"pause": False}   # set by the Pause button; checked after every node


def _config():
    return {"configurable": {"thread_id": sq.STATE["id"]}}


def _drive(graph_input):
    """Run the graph until it finishes or a pause is requested. LangGraph saves a checkpoint after
    every node, so stopping between nodes loses nothing; Continue resumes from that checkpoint."""
    final = {}
    for update in SQUAD_GRAPH.stream(graph_input, config=_config(), stream_mode="values"):
        final = update
        sq.STATE["graph_trace"] = update.get("trace", [])
        if CONTROL["pause"] and SQUAD_GRAPH.get_state(_config()).next:
            nxt = SQUAD_GRAPH.get_state(_config()).next[0]
            sq.STATE.update(paused=True, pause_requested=False)
            _log(f"LangGraph run paused at a checkpoint - next step: {nxt}. Shared memory is kept; press Continue.",
                 "AI")
            return {**final, "paused": True}
    return final


def run_squad(ingestor, log, cfg, kube, stop):
    RUNTIME.update(ingestor=ingestor, log=log, cfg=cfg, kube=kube, stop=stop, faults=[], hold=None, errors=[])
    CONTROL["pause"] = False
    return _drive({"run_id": sq.STATE["id"], "board": [], "trace": []})


def resume_squad(log):
    """Continue the paused run from its last checkpoint (same thread_id = same memory)."""
    RUNTIME["log"] = log
    CONTROL["pause"] = False
    sq.STATE.update(paused=False, pause_requested=False)
    nxt = SQUAD_GRAPH.get_state(_config()).next
    _log(f"LangGraph run continuing from its checkpoint at: {nxt[0] if nxt else 'end'}.", "AI")
    return _drive(None)


def checkpoint_info():
    """What LangGraph has saved for the current run: next step, team board, number of checkpoints."""
    if not sq.STATE.get("id"):
        return None
    try:
        snap = SQUAD_GRAPH.get_state(_config())
    except Exception:  # noqa: BLE001
        return None
    if not snap or not snap.values:
        return None
    history = list(SQUAD_GRAPH.get_state_history(_config()))
    return {"thread_id": sq.STATE["id"], "checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
            "next": list(snap.next), "checkpoints_saved": len(history),
            "board_entries": len(snap.values.get("board", [])), "trace": snap.values.get("trace", []),
            "memory_recalled": bool(snap.values.get("recall"))}

def run_analysis(ingestor, log):
    """Re-run ranking + fixes + memory on the latest telemetry (the 'Re-run analysis' button)."""
    RUNTIME.update(ingestor=ingestor, log=log)
    ANALYSIS_GRAPH.invoke({"run_id": sq.STATE["id"], "board": list(sq.STATE.get("board", [])), "trace": []})
