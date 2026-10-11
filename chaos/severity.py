"""Severity ranking of the observed errors - a separate step from injection and from fixes.

1. measure(): what each error actually did to shoppers, from live telemetry.
2. formula_score(): a transparent 0-100 score from those measurements (same input -> same score).
3. rank_by_severity(): the analyst model ranks the errors, given the measurements and the formula
   scores, and explains its order. Its answer must be a valid ranking of exactly these errors;
   otherwise the formula ranking is used and the result says so.
"""
import json

from pydantic import BaseModel, Field

from .analyst import ALL_PAGES, PAGES, ask, describe
from .llm import LLMError

WEIGHTS = {"user_impact": 0.40, "business": 0.25, "blast_radius": 0.20, "slowdown": 0.15}
JOURNEY_VALUE = {"checkout": 1.0, "cart": 0.7, "browse": 0.6}   # what a broken journey costs the shop
LABELS = [(70, "Critical"), (40, "High"), (20, "Medium"), (0, "Low")]
TIE_MARGIN = 10   # score points within which the analyst model may reorder errors


def label(score):
    return next(name for floor, name in LABELS if score >= floor)


def measure(ingestor, target, since, until=None):
    """Impact attributed to `target` by telemetry between since and until."""
    until = until or 1e18
    errs = [e for e in list(ingestor.errors) if since <= e["t"] <= until and e["service"] == target]
    total = sum(m["n"] for m in list(ingestor.metrics) if since <= m["t"] <= until) or 0

    def count(pred):
        return sum(e["count"] for e in errs if pred(e["type"]))

    failed = count(lambda t: t != "High latency" and not t.startswith("Degraded"))
    slow = count(lambda t: t == "High latency")
    degraded = count(lambda t: t.startswith("Degraded"))
    pages = lambda pred: sorted({PAGES.get(s, s) for e in errs if pred(e["type"]) for s in e.get("steps", [])},
                                key=lambda p: ALL_PAGES.index(p) if p in ALL_PAGES else 99)
    hard = lambda t: not t.startswith("Degraded")
    msgs = list(dict.fromkeys(e["message"] for e in errs if hard(e["type"])))[:3]
    return {"requests_total": total, "failed_requests": failed, "slow_requests": slow,
            "degraded_requests": degraded,
            "failure_rate": round(failed / total, 4) if total else 0.0,
            "slow_rate": round(slow / total, 4) if total else 0.0,
            "pages_failed": pages(hard), "pages_degraded": pages(lambda t: t.startswith("Degraded")),
            "journeys_failed": sorted({j for e in errs if hard(e["type"]) for j in e.get("journeys", [])}),
            "sample_messages": msgs}


def formula_score(impact):
    """0-100. Each factor is 0..1; see WEIGHTS."""
    factors = {
        # 25% of all shopper requests failing because of one fault is as bad as it gets here.
        "user_impact": min(1.0, impact["failure_rate"] / 0.25),
        "business": max([JOURNEY_VALUE.get(j, 0.5) for j in impact["journeys_failed"]]
                        or [0.15 if impact["degraded_requests"] or impact["slow_requests"] else 0.0]),
        "blast_radius": len(impact["pages_failed"]) / len(ALL_PAGES),
        "slowdown": min(1.0, impact["slow_rate"] / 0.25),
    }
    score = round(100 * sum(WEIGHTS[k] * v for k, v in factors.items()))
    return score, {k: round(v, 2) for k, v in factors.items()}


class RankedError(BaseModel):
    error_id: str = Field(description="error_id from the input")
    rank: int = Field(description="1 = most severe", json_schema_extra={"minimum": 1, "maximum": 3})
    severity: str = Field(description="Critical, High, Medium or Low",
                          json_schema_extra={"enum": ["Critical", "High", "Medium", "Low"]})
    justification: str = Field(description="One or two sentences citing the observed numbers")


class Ranking(BaseModel):
    ranking: list[RankedError]


SYSTEM = """You are a site-reliability analyst. Rank the given production errors from most to least
severe for the business and its users, using ONLY the observed measurements provided.
Consider, in this order: how many shopper requests failed, whether checkout/payment (revenue) is
broken, how many pages are affected, and slowness. A fault that users cannot notice is Low.
formula_score is computed from the same numbers (labels: 70+ Critical, 40-69 High, 20-39 Medium, under 20
Low); follow it unless the evidence clearly says otherwise, and say why if you deviate. A few failed
requests out of many is minor. Every error_id must appear exactly once."""


def rank_by_severity(errors):
    """errors: [{agent, target, failure_type, params, hypothesis, impact}] -> ranked list (rank 1 first)."""
    scored = []
    for e in errors:
        score, factors = formula_score(e["impact"])
        scored.append({**e, "formula_score": score, "formula_label": label(score), "factors": factors})
    by_formula = sorted(scored, key=lambda e: -e["formula_score"])
    for i, e in enumerate(by_formula, 1):
        e["formula_rank"] = i

    payload = [{**describe(e), "formula_score": e["formula_score"]} for e in scored]
    model, note = None, None
    try:
        data, model = ask(SYSTEM, "Rank these errors:\n" + json.dumps(payload, indent=1),
                          Ranking.model_json_schema())
        ranking = Ranking.model_validate(data).ranking
        ids = [r.error_id for r in ranking]
        if sorted(ids) != sorted(e["agent"] for e in scored) or sorted(r.rank for r in ranking) != \
                list(range(1, len(scored) + 1)):
            raise ValueError(f"not a valid ranking of these errors: {[(r.error_id, r.rank) for r in ranking]}")
        by_id = {r.error_id: r for r in ranking}
        # The severity label always comes from the measured score, so the label, the score bar and the
        # resilience score never contradict each other; the model's own label is kept for transparency.
        for e in scored:
            r = by_id[e["agent"]]
            e.update(rank=r.rank, severity=e["formula_label"], model_severity=r.severity,
                     justification=r.justification, ranked_by=model)
        # The model may reorder near-ties (it can weigh evidence the formula cannot), but not put an error
        # above one whose measured score is clearly higher.
        swapped = [(a["target"], b["target"]) for a in scored for b in scored
                   if a["rank"] < b["rank"] and b["formula_score"] - a["formula_score"] > TIE_MARGIN]
        if swapped:
            note = (f"analyst order contradicted the measurements ({swapped[0][0]} above {swapped[0][1]}); "
                    f"kept its explanations but used the measured order")
            for e in scored:
                e["rank"] = e["formula_rank"]
    except (LLMError, ValueError) as ex:
        note = f"analyst model ranking unusable ({str(ex)[:160]}); using the formula ranking"
        for e in scored:
            e.update(rank=e["formula_rank"], severity=e["formula_label"], ranked_by="formula",
                     justification=_formula_reason(e))
    return {"ranked": sorted(scored, key=lambda e: e["rank"]), "model": model, "note": note,
            "weights": WEIGHTS}


def _formula_reason(e):
    m = e["impact"]
    pages = ", ".join(m["pages_failed"]) or "no pages"
    return (f"{m['failed_requests']} failed of {m['requests_total']} requests "
            f"({100 * m['failure_rate']:.1f}%), broke {pages}"
            + (f", {m['slow_requests']} slow" if m["slow_requests"] else "")
            + (f", {m['degraded_requests']} degraded" if m["degraded_requests"] else "") + ".")
