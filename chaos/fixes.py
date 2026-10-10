"""Fix recommendations for each observed error, written by the analyst model.

Advice only: the text is shown to people and never executed.
"""
import json

from pydantic import BaseModel, Field

from .analyst import ask, describe
from .llm import LLMError


class Fix(BaseModel):
    likely_cause: str = Field(description="Why users saw these symptoms, in one or two sentences")
    immediate_fix: str = Field(description="What an operator should do right now to restore service")
    long_term_fix: str = Field(description="Engineering change so this failure hurts less next time")
    prevention: str = Field(description="Monitoring, alert or test that would catch this early")
    confidence: str = Field(description="high, medium or low", json_schema_extra={"enum": ["high", "medium", "low"]})


SYSTEM = """You are a senior site-reliability engineer for an online shop running as Kubernetes
microservices (Google's Online Boutique: a Go frontend calling gRPC services; the cart is stored in
redis-cart). A chaos experiment injected the error below on purpose and live telemetry recorded what
shoppers saw. Recommend fixes grounded in that evidence. Be concrete and specific to this service
(replicas, readiness probes, timeouts, retries with backoff, graceful degradation/fallbacks, circuit
breakers, caching, alerting). Keep each field under 60 words. Do not invent measurements."""

CONTEXT = {
    "service-down": "the deployment was scaled to 0 replicas (complete outage of this service)",
    "pod-kill": "its pods were force-killed repeatedly (crash loop) while Kubernetes kept restarting them",
    "network-delay": "a fixed network delay was added to every call into this service",
}


def recommend(err):
    """One error -> {fix fields..., model}. Raises LLMError if no analyst model answers."""
    user = (f"Injected fault: {CONTEXT.get(err['failure_type'], err['failure_type'])}.\n"
            "Error and observed impact:\n" + json.dumps(describe(err), indent=1))
    data, model = ask(SYSTEM, user, Fix.model_json_schema())
    return {**Fix.model_validate(data).model_dump(), "model": model}


def recommend_all(errors, on_done=None):
    """Fixes for every error, one model call each. A failure is recorded per error."""
    out = {}
    for e in errors:
        try:
            out[e["agent"]] = recommend(e)
        except (LLMError, ValueError) as ex:
            out[e["agent"]] = {"error": str(ex)[:300]}
        if on_done:
            on_done(e["agent"], out[e["agent"]])
    return out
