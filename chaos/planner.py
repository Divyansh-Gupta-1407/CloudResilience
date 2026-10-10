"""AI planner: asks Claude to rank experiments, then gates every proposal through validate().

The model only returns structured data (target, failure type, duration, hypothesis,
reason). It never produces commands; execution is done by the controller's fault
templates after validation.
"""
import glob
import json
import os
import time
from pathlib import Path

import anthropic
import requests
from pydantic import BaseModel, Field, ValidationError as SchemaError

from .controller import OUT, ValidationError, load_safety, validate
from .discovery import DEFAULT_MANIFEST, discover
from .faults import CATALOG

CLAUDE_MODEL = "claude-opus-5-5"
EFFORT = "medium"
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CHAOS_OLLAMA_MODEL", "qwen3:4b")
PROMPT_PATH = Path(__file__).with_name("prompts") / "planner.md"


def backend():
    """CHAOS_LLM=claude|ollama overrides; otherwise Claude when a key is set, else local Ollama."""
    choice = os.environ.get("CHAOS_LLM")
    if choice:
        return choice
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") else "ollama"


class Proposal(BaseModel):
    rank: int = Field(description="1 = run first")
    target: str = Field(description="Deployment name from allowed_targets")
    failure_type: str = Field(description="Key from fault_catalog")
    # minimum/maximum go into the JSON schema only (json_schema_extra), so a local model's
    # constrained decoding respects them; enforcement stays with the controller's validate().
    duration_s: int = Field(description="Fault duration in seconds, 5-30 (hard cap 30)",
                            json_schema_extra={"minimum": 5, "maximum": 30})
    delay_ms: int = Field(description="network-delay only: latency added to every call into the target, "
                                      "50-3000 ms. Use 0 for other failure types.",
                          json_schema_extra={"minimum": 0, "maximum": 3000})
    hypothesis: str = Field(description="Specific, falsifiable expected behaviour")
    reason: str = Field(description="Why this ranks here, citing map/baseline/prior-run evidence")


class Plan(BaseModel):
    experiments: list[Proposal]


class ModelUnavailable(RuntimeError):
    pass


def _prior_runs(limit=20):
    """Compact summaries of past runs so the planner can learn from them."""
    runs = []
    for path in sorted(glob.glob(str(OUT / "runs" / "*.json")))[-limit:]:
        r = json.loads(Path(path).read_text())
        exp = r.get("experiment") or r.get("request") or {}
        runs.append({
            "run_id": r.get("run_id"), "target": exp.get("target"), "failure_type": exp.get("failure_type"),
            "status": r.get("status"), "stop_reason": r.get("stop_reason"),
            "during_fault": r.get("during_fault"), "recovery": r.get("recovery"),
            "data_check": r.get("data_check"),
        })
    return runs


def build_context():
    cfg = load_safety()
    dep = discover(DEFAULT_MANIFEST)
    baseline_path = OUT / "baseline.json"
    return {
        "dependency_map": {
            "services": {n: {k: s.get(k) for k in ("calls", "callers", "critical", "role", "deployed")}
                         for n, s in dep["services"].items()},
            "journeys": dep["journeys"],
        },
        "baseline": json.loads(baseline_path.read_text()) if baseline_path.exists() else None,
        "fault_catalog": {name: {"description": " ".join((f.__doc__ or "").split()),
                                 "params": {k: {"min": lo, "max": hi, "default": d}
                                            for k, (lo, hi, d) in f.params_schema.items()}}
                          for name, f in CATALOG.items()},
        "allowed_targets": cfg["allowed_targets"],
        "max_fault_duration_s": cfg["max_fault_duration_s"],
        "prior_runs": _prior_runs(),
    }


def _user_prompt(context):
    return ("Rank the next chaos experiments using this context:\n\n"
            + json.dumps(context, indent=1, default=str))


def ask_model(context, on_thinking=None):
    """Returns (Plan, usage dict). Raises ModelUnavailable on any model/format failure.

    on_thinking(text) receives the model's reasoning as it streams (local model only).
    """
    return _ask_claude(context) if backend() == "claude" else _ask_ollama(context, on_thinking=on_thinking)


def _ask_ollama(context, show_thinking=True, on_thinking=None):
    body = {
        "model": OLLAMA_MODEL,
        "stream": True,
        "think": True,                       # reason first; the answer is still schema-constrained
        "format": Plan.model_json_schema(),  # Ollama constrains decoding to this JSON schema
        # ~3k-token prompt + reasoning + answer fits in 8k; a larger context spills a
        # 4B model off a 6 GB laptop GPU onto the CPU and makes planning ~5x slower.
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [{"role": "system", "content": PROMPT_PATH.read_text()},
                     {"role": "user", "content": _user_prompt(context)}],
    }
    print(f"Asking {OLLAMA_MODEL} (local, GPU) to rank experiments. Its reasoning streams below; "
          f"this usually takes 2-3 minutes.\n", flush=True)
    content, thinking, last = [], [], {}
    try:
        with requests.post(f"{OLLAMA_URL}/api/chat", json=body, stream=True, timeout=(10, 300)) as r:
            if r.status_code != 200:
                raise ModelUnavailable(f"Ollama error {r.status_code}: {r.text[:300]}")
            phase = None
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                    if on_thinking:
                        on_thinking(msg["thinking"])
                    if show_thinking:
                        if phase != "think":
                            print("  [AI thinking] ", end="", flush=True)
                            phase = "think"
                        print(msg["thinking"], end="", flush=True)
                if msg.get("content"):
                    content.append(msg["content"])
                    if phase != "answer":
                        print("\n\n  [AI writing its ranked plan...]", flush=True)
                        phase = "answer"
                if chunk.get("done"):
                    last = chunk
    except requests.RequestException as e:
        raise ModelUnavailable(f"cannot reach Ollama at {OLLAMA_URL} (is it running?): {e}") from e
    print(flush=True)
    try:
        plan = Plan.model_validate_json("".join(content))
    except SchemaError as e:
        raise ModelUnavailable(f"model output did not match the plan schema: {e}") from e
    usage = {k: last.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration")}
    usage["thinking_chars"] = len("".join(thinking))
    return plan, usage


def _ask_claude(context):
    print(f"Asking {CLAUDE_MODEL} to rank experiments (usually under a minute)...", flush=True)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            output_config={"effort": EFFORT},
            system=PROMPT_PATH.read_text(),
            messages=[{"role": "user", "content": _user_prompt(context)}],
            output_format=Plan,
        )
    except anthropic.AuthenticationError as e:
        raise ModelUnavailable(f"authentication failed (set ANTHROPIC_API_KEY): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ModelUnavailable(f"cannot reach the Claude API: {e}") from e
    except anthropic.APIStatusError as e:
        raise ModelUnavailable(f"Claude API error {e.status_code}: {e.message}") from e
    except TypeError as e:  # raised by the SDK when no credentials are configured at all
        raise ModelUnavailable(f"no Claude credentials configured: {e}") from e

    if resp.stop_reason == "refusal":
        raise ModelUnavailable("model declined the request")
    if resp.parsed_output is None:
        raise ModelUnavailable(f"model returned no valid plan (stop_reason={resp.stop_reason})")
    return resp.parsed_output, resp.usage.to_dict()


def gate(proposals, cfg):
    """Run every proposal through the controller's validator. Nothing unvalidated survives."""
    results = []
    for p in sorted(proposals, key=lambda x: x["rank"]):
        p = {**p, "params": {"delay_ms": p.get("delay_ms")} if p["failure_type"] == "network-delay" else {}}
        try:
            validate(p, cfg)
            results.append({**p, "verdict": "accepted"})
        except ValidationError as e:
            results.append({**p, "verdict": "rejected", "rejection": str(e)})
    return results


def make_plan(on_thinking=None):
    """Returns a plan record (always saved). status: ok | model_unavailable."""
    cfg = load_safety()
    plan_id = time.strftime("%Y%m%d-%H%M%S")
    be = backend()
    record = {"plan_id": plan_id, "backend": be,
              "model": CLAUDE_MODEL if be == "claude" else OLLAMA_MODEL,
              "settings": {"effort": EFFORT} if be == "claude" else {"think": True, "temperature": 0.2},
              "prompt_file": str(PROMPT_PATH.name)}
    context = build_context()
    record["context_summary"] = {"prior_runs": len(context["prior_runs"]),
                                 "baseline_p95_ms": (context["baseline"] or {}).get("overall", {}).get("p95_ms")}
    try:
        plan, usage = ask_model(context, on_thinking=on_thinking)
        record.update(status="ok", usage=usage,
                      proposals=gate([p.model_dump() for p in plan.experiments], cfg))
    except ModelUnavailable as e:
        record.update(status="model_unavailable", error=str(e), proposals=[])

    (OUT / "plans").mkdir(parents=True, exist_ok=True)
    (OUT / "plans" / f"{plan_id}.json").write_text(json.dumps(record, indent=2, default=str))
    return record


def latest_plan():
    files = sorted(glob.glob(str(OUT / "plans" / "*.json")))
    return json.loads(Path(files[-1]).read_text()) if files else None


def run_plan(plan, top=1, services=None, on_start=None, on_result=None, hold=False):
    """Execute the accepted proposals of a plan in rank order, each through run_experiment.

    services: optional allowlist subset to restrict which targets may be run.
    hold: inject only the top pick and keep it active until rollback / kill switch.
    Stops early if the kill switch is turned on. Returns the run records.
    """
    if hold:
        top = 1
    from .controller import kill_switch_path, run_experiment
    cfg = load_safety()
    accepted = [p for p in plan["proposals"] if p["verdict"] == "accepted"
                and (not services or p["target"] in services)][:top]
    records = []
    for p in accepted:
        if kill_switch_path(cfg).exists():
            break
        if on_start:
            on_start(p)
        exp = {**p, "plan_id": plan["plan_id"]}
        if hold:
            exp.update(hold=True, duration_s=None)
        rec = run_experiment(exp)
        records.append(rec)
        if on_result:
            on_result(p, rec)
    return records
