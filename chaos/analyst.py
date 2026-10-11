"""The analyst model: a separate Ollama model (not one of the three injecting agents) that reviews
the errors after they have been observed. Used by severity.py (ranking) and fixes.py (repairs).

Default: gemma3:4b, a local model from a different family than the three agents (qwen3, ministral,
llama3.2). Optional: a larger Ollama cloud model, e.g.
    ollama signin
    $env:CHAOS_ANALYST_MODEL = "gemma4:31b-cloud"
If the preferred model is unavailable, the fallback is used and the UI says why.
"""
import os

from .llm import LLMError, chat_json, installed_models

ANALYST_MODEL = os.environ.get("CHAOS_ANALYST_MODEL", "gemma3:4b")
FALLBACK_MODEL = os.environ.get("CHAOS_ANALYST_FALLBACK", "qwen3:4b")

# Shop pages, as the telemetry probe names its steps.
PAGES = {"browse/home": "home", "browse/product": "product", "cart/add": "add to cart",
         "checkout/add": "add to cart", "cart/view": "view cart", "checkout/place_order": "checkout"}
ALL_PAGES = ["home", "product", "view cart", "add to cart", "checkout"]

_state = {"model": None, "note": None}   # the model that actually answered last


def _why_unavailable(model, err):
    if "401" in err or "Unauthorized" in err:
        return f"{model} is an Ollama cloud model and needs `ollama signin`"
    return f"{model} failed ({err[:100]})"


def ask(system, user, schema, think=False):
    """Ask the analyst model; fall back to the local model if the preferred one fails.
    Returns (data, model_used)."""
    have = installed_models()
    reasons = []
    for model in dict.fromkeys([ANALYST_MODEL, FALLBACK_MODEL]):
        if have and model not in have and f"{model}:latest" not in have:
            reasons.append(f"{model} is not installed (ollama pull {model})")
            continue
        try:
            data, _ = chat_json(model, system, user, schema, think=think and model.startswith("qwen3"),
                                temperature=0.2, timeout_s=240)
            _state.update(model=model, note=None if model == ANALYST_MODEL else
                          f"{'; '.join(reasons)} - used {model} instead")
            return data, model
        except LLMError as e:
            reasons.append(_why_unavailable(model, str(e)))
    raise LLMError("analyst models unavailable: " + "; ".join(reasons))


def status():
    return {"preferred": ANALYST_MODEL, "fallback": FALLBACK_MODEL, **_state}


def describe(err):
    """Compact, model-friendly description of one observed error (see severity.measure)."""
    m = err.get("impact") or {}
    return {"error_id": err["agent"], "service": err["target"], "fault": err["failure_type"],
            "params": err.get("params") or {}, "agent_hypothesis": err.get("hypothesis"),
            "agent_note_on_team_board": (err.get("team_note") or {}).get("message"),
            "past_results_same_service_and_fault": [{k: t.get(k) for k in ("run", "score", "severity", "failure_rate", "at")}
                                                    for t in err.get("history") or []],
            "observed": {k: m.get(k) for k in ("requests_total", "failed_requests", "slow_requests",
                                               "degraded_requests", "failure_rate", "pages_failed",
                                               "pages_degraded", "journeys_failed", "sample_messages")}}
