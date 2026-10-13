"""Incident reports built from a squad run, the record that goes to S3 and SNS.

Input is the squad state as chaos/squad.py keeps it (squad.public_state(), also stored in the
squad_runs table), so the same function works for the live run and for any past run in History.
The score and labels use the same formulas as chaos/api.py /findings.
"""
from datetime import datetime, timezone

SCHEMA_VERSION = 1
FAULT_LABEL = {"service-down": "Outage", "pod-kill": "Crash loop", "network-delay": "Network latency"}
FAULT_CAUSE = {"service-down": "service outage (scaled to 0 replicas)", "pod-kill": "crash loop (pods killed repeatedly)",
               "network-delay": "network latency on every call into the service"}
SEVERITY_ORDER = ["Critical", "High", "Medium", "Low"]


def resilience_score(ranked):
    scores = [e["formula_score"] for e in ranked]
    if not scores:
        return None
    return round(100 - (0.6 * max(scores) + 0.4 * sum(scores) / len(scores)))


def score_label(score):
    if score is None:
        return "Not analysed"
    return "Resilient" if score >= 70 else "Fragile" if score >= 40 else "Not resilient"


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def build_report(squad_state, rollback=None, environment="sandbox", generated_at=None):
    """A self-contained incident report (plain JSON-serialisable dict)."""
    sv = squad_state.get("severity") or {}
    ranked = sv.get("ranked") or []
    fixes = squad_state.get("fixes") or {}
    score = resilience_score(ranked)
    findings = []
    for e in ranked:
        params = e.get("params") or {}
        cause = f"{FAULT_CAUSE.get(e['failure_type'], e['failure_type'])} on {e['target']}"
        if params.get("delay_ms"):
            cause += f" (+{params['delay_ms']} ms)"
        fix = fixes.get(e["agent"])
        findings.append({"rank": e["rank"], "severity": e["severity"], "formula_score": e["formula_score"],
                         "target": e["target"], "failure_type": e["failure_type"], "params": params,
                         "agent": e["agent"], "agent_model": e.get("model"), "justification": e.get("justification"),
                         "impact": e.get("impact") or {}, "suspected_cause": cause,
                         "fix": fix if fix and not fix.get("error") else None})
    injected = [{"agent": a["id"], "model": a.get("model"), "role": a.get("name"),
                 "target": (a.get("pick") or {}).get("target"), "failure_type": (a.get("pick") or {}).get("failure_type"),
                 "params": (a.get("pick") or {}).get("params") or {}, "applied": bool(a.get("applied")),
                 "fallback": bool(a.get("fallback")), "status": a.get("status")}
                for a in squad_state.get("agents", [])]
    worst = next((s for s in SEVERITY_ORDER if any(f["severity"] == s for f in findings)), None)
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "chaos-squad-incident",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": environment,
        "squad_id": squad_state["id"],
        "squad_status": squad_state.get("status"),
        "started_at": _iso(squad_state.get("started")),
        "injected_at": _iso(squad_state.get("injected_at")),
        "resilience_score": score,
        "resilience_label": score_label(score),
        "worst_severity": worst,
        "analyst_model": sv.get("model") or ("formula" if ranked else None),
        "ranking_note": sv.get("note"),
        "faults_injected": injected,
        "findings": findings,
        "rollback": rollback,
    }


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


def to_markdown(r):
    rb = r.get("rollback")
    rb_text = ("none recorded" if not rb else
               f"recovery verified in {rb.get('recovery_time_s')} s" if rb.get("recovery_verified")
               else "recovery NOT verified")
    lines = [f"# CloudResilience incident report: squad {r['squad_id']}", "",
             f"- Environment: `{r['environment']}`",
             f"- Generated: {r['generated_at']}",
             f"- Squad status: {r.get('squad_status')}",
             f"- Resilience score: **{r['resilience_score'] if r['resilience_score'] is not None else 'n/a'} / 100** "
             f"({r['resilience_label']})",
             f"- Worst severity: {r.get('worst_severity') or 'n/a'}",
             f"- Ranked by: {r.get('analyst_model') or 'n/a'}" + (f" ({r['ranking_note']})" if r.get("ranking_note") else ""),
             f"- Rollback: {rb_text}", "", "## Faults injected", "",
             "| Agent | Model | Service | Fault | Applied |", "|---|---|---|---|---|"]
    for f in r["faults_injected"]:
        delay = f" +{f['params']['delay_ms']} ms" if f["params"].get("delay_ms") else ""
        lines.append(f"| {f['agent']} | {f.get('model') or ''} | {f.get('target') or '-'} | "
                     f"{FAULT_LABEL.get(f.get('failure_type'), f.get('failure_type') or '-')}{delay} | "
                     f"{'yes' if f['applied'] else 'no'}{' (fallback)' if f['fallback'] else ''} |")
    if not r["findings"]:
        lines += ["", "_No severity ranking: the run was not analysed before it ended._"]
        return "\n".join(lines) + "\n"
    lines += ["", "## Findings", "", "| Rank | Severity | Service | Fault | Failed requests | Pages broken | Score |",
              "|---|---|---|---|---|---|---|"]
    for f in r["findings"]:
        i = f["impact"]
        lines.append(f"| {f['rank']} | {f['severity']} | {f['target']} | "
                     f"{FAULT_LABEL.get(f['failure_type'], f['failure_type'])} | "
                     f"{i.get('failed_requests', 0)}/{i.get('requests_total', 0)} ({_pct(i.get('failure_rate'))}) | "
                     f"{', '.join(i.get('pages_failed') or []) or 'none'} | {f['formula_score']} |")
    for f in r["findings"]:
        i = f["impact"]
        lines += ["", f"### #{f['rank']} {f['severity']}: {f['target']} ({FAULT_LABEL.get(f['failure_type'], f['failure_type'])})",
                  "", f"**Measured impact:** {i.get('failed_requests', 0)} failed, {i.get('slow_requests', 0)} slow, "
                  f"{i.get('degraded_requests', 0)} degraded of {i.get('requests_total', 0)} requests.",
                  "", f"**Suspected cause:** {f['suspected_cause']}", "", f"**Why this rank:** {f.get('justification') or 'n/a'}"]
        fix = f.get("fix")
        if fix:
            lines += ["", f"**Recommended fix** ({fix.get('model')}, confidence {fix.get('confidence')})", "",
                      f"- Likely cause: {fix.get('likely_cause')}", f"- Immediate fix: {fix.get('immediate_fix')}",
                      f"- Long-term fix: {fix.get('long_term_fix')}", f"- Prevention: {fix.get('prevention')}"]
        else:
            lines += ["", "**Recommended fix:** not available"]
    return "\n".join(lines) + "\n"


def summary_text(r, report_url=None):
    """Short plain-text summary for SNS (email / SMS / chat subscribers)."""
    lines = [f"CloudResilience squad {r['squad_id']} ({r['environment']})",
             f"Resilience score: {r['resilience_score'] if r['resilience_score'] is not None else 'n/a'}/100 "
             f"({r['resilience_label']})"]
    for f in r["findings"]:
        i = f["impact"]
        lines.append(f"#{f['rank']} {f['severity']}: {FAULT_LABEL.get(f['failure_type'], f['failure_type'])} on "
                     f"{f['target']}, {_pct(i.get('failure_rate'))} of requests failed")
    if not r["findings"]:
        lines.append("Run ended before the severity analysis.")
    rb = r.get("rollback")
    if rb:
        lines.append("Rollback: " + (f"recovery verified in {rb.get('recovery_time_s')} s" if rb.get("recovery_verified")
                                     else "recovery NOT verified - check the cluster"))
    if report_url:
        lines.append(f"Full report: {report_url}")
    return "\n".join(lines)
