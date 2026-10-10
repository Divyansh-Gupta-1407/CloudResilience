import os
import random
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

# =========================================================
# RESILIENCEAI — COMPLETE DASHBOARD
# API mode is wired to the chaos backend (chaos/api.py): live telemetry, the
# errors the AI agent induces, experiment control, and verified rollback.
# =========================================================

st.set_page_config(
    page_title="ResilienceAI | Control Center",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_URL = os.getenv("RESILIENCE_API_URL", "http://127.0.0.1:8000")
LIVE_REFRESH = "2s"

SERVICES = [
    "frontend",
    "cartservice",
    "checkoutservice",
    "paymentservice",
    "currencyservice",
    "productcatalogservice",
    "recommendationservice",
    "shippingservice",
    "emailservice",
    "adservice",
    "redis-cart",
]

FAULT_TYPES = {
    "Latency injection": "latency",
    "Pod failure": "pod_failure",
    "Service unavailability": "service_unavailability",
}

ERROR_TYPES = [
    {"type": "Timeout", "message": "Request exceeded the response time limit"},
    {"type": "HTTP 500", "message": "Internal server error reported"},
    {"type": "Connection error", "message": "Connection attempt failed"},
    {"type": "High latency", "message": "Response latency exceeded threshold"},
    {"type": "Dependency error", "message": "Downstream dependency unavailable"},
]

# =========================================================
# STYLING
# =========================================================

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    .hero {
        padding: 1.5rem;
        border: 1px solid rgba(128,128,128,.3);
        border-radius: 16px;
        margin-bottom: 1rem;
    }
    .subtle { opacity: .7; font-size: .9rem; }
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,.25);
        padding: 14px;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================================================
# SESSION STATE (demo mode only; API mode reads everything from the backend)
# =========================================================

DEFAULTS = {
    "ingestion_running": False,
    "experiment_status": "IDLE",
    "experiment_id": None,
    "experiment_history": [],
    "events": [],
    "active_errors": [],
    "error_history": [],
    "metric_history": [],
    "baseline_saved": False,
    "rollback_verified": False,
    "random_injection_active": False,
    "random_injection_count": 0,
    "baseline_latency": 82.0,
    "baseline_error_rate": 0.2,
    "current_latency": 82.0,
    "current_error_rate": 0.2,
    "last_rollback": None,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(message, category="INFO"):
    st.session_state.events.insert(0, {"timestamp": now_iso(), "category": category, "event": message})


def record_metrics():
    """Record illustrative local demo metrics."""
    st.session_state.metric_history.append(
        {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "latency_ms": round(st.session_state.current_latency, 2),
            "error_rate_pct": round(st.session_state.current_error_rate, 2),
        }
    )
    st.session_state.metric_history = st.session_state.metric_history[-50:]


def api_get(path, timeout=5):
    response = requests.get(f"{API_URL}{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_post(path, payload=None, timeout=30):
    response = requests.post(f"{API_URL}{path}", json=payload or {}, timeout=timeout)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise requests.HTTPError(f"{response.status_code}: {detail}")
    if response.content:
        return response.json()
    return {"status": "accepted"}


def run_api_action(label, operation):
    try:
        result = operation()
        st.success(f"{label} request completed.")
        return result
    except requests.RequestException as exc:
        st.error(f"{label} failed: {exc}")
        return None


def safe_get(path, default=None, timeout=5):
    try:
        return api_get(path, timeout=timeout)
    except requests.RequestException:
        return default


def pct(x):
    return "—" if x is None else f"{100 * x:.1f}%"


# =========================================================
# SIDEBAR
# =========================================================

st.sidebar.title("🛡️ ResilienceAI")
st.sidebar.caption("Cloud Resilience Control Center")
st.sidebar.divider()

mode = st.sidebar.radio("Operating mode", ["API mode", "Demo mode"])
API = mode == "API mode"

st.sidebar.markdown("### Environment")
st.sidebar.write("Google Online Boutique")
st.sidebar.code("Kubernetes (kind) · Docker Desktop")

backend_status = safe_get("/status") if API else None

if not API:
    st.sidebar.info("Demo mode uses simulated events and metrics. It does not inject faults into Kubernetes.")
elif backend_status is None:
    st.sidebar.error(f"Backend not reachable at `{API_URL}`.")
    st.sidebar.caption("Start it with: `python -m uvicorn chaos.api:app --port 8000`")
else:
    st.sidebar.success(f"Backend connected: `{API_URL}`")
    st.sidebar.write(f"AI planner: **{backend_status['ai_backend']}**")
    if backend_status["kill_switch"]:
        st.sidebar.warning("Kill switch is ON")

st.sidebar.divider()
st.sidebar.markdown("### Safety")
st.sidebar.write("✓ Target allowlist (namespace `boutique` only)")
st.sidebar.write("✓ Faults hold until Rollback (10-min safety backstop)")
st.sidebar.write("✓ Kill switch freezes the current state")
st.sidebar.write("✓ Rollback restores the initial state and verifies recovery")
st.sidebar.write("✓ AI proposals validated before execution")

# =========================================================
# HEADER
# =========================================================

st.markdown(
    """
    <div class="hero">
      <h1 style="margin:0">ResilienceAI</h1>
      <p style="margin:.5rem 0">Ingest telemetry. Let the AI inject faults. Measure impact. Recover.</p>
      <p class="subtle" style="margin:0">Google Online Boutique · Resilience Experiment Dashboard</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not API:
    st.info("You are viewing a local simulation. Switch to API mode to drive the real agent.")
elif backend_status is None:
    st.error("The backend is not running, so live controls are unavailable. Start it and refresh this page.")
    st.stop()

# =========================================================
# 1. INGESTION CONTROLS
# =========================================================

st.subheader("1. Telemetry ingestion")

start_col, stop_col, status_col = st.columns([1, 1, 2])
with start_col:
    start_ingestion = st.button("▶ Start ingestion", type="primary", width="stretch")
with stop_col:
    stop_ingestion = st.button("■ Stop ingestion", width="stretch")

if start_ingestion:
    if API:
        run_api_action("Start ingestion", lambda: api_post("/ingestion/start"))
    else:
        st.session_state.ingestion_running = True
        st.session_state.baseline_saved = True
        st.session_state.rollback_verified = False
        st.session_state.current_latency = st.session_state.baseline_latency
        st.session_state.current_error_rate = st.session_state.baseline_error_rate
        record_metrics()
        log_event("Demo ingestion started; initial demo baseline captured.")
        st.success("Demo ingestion started. No live telemetry is collected.")

if stop_ingestion:
    if API:
        run_api_action("Stop ingestion", lambda: api_post("/ingestion/stop"))
    else:
        st.session_state.ingestion_running = False
        log_event("Demo ingestion stopped. Existing records retained.")
        st.success("Ingestion stopped; recorded history is preserved.")

with status_col:
    if API:
        s = safe_get("/status") or {}
        st.metric("Ingestion status", s.get("ingestion", "UNKNOWN"))
    else:
        st.metric("Ingestion status", "RUNNING" if st.session_state.ingestion_running else "STOPPED")

st.caption(
    "Ingestion sends light, steady synthetic shopper traffic through the shop and measures every request. "
    "Stopping it does not stop a running fault or roll back an experiment."
)

# =========================================================
# 2. EXPERIMENT CONTROLS (manual)
# =========================================================

st.divider()
st.subheader("2. Experiment control")

left, right = st.columns([1.1, 0.9], gap="large")

with left:
    with st.form("experiment_form"):
        target = st.selectbox("Target service", SERVICES)
        fault_label = st.selectbox("Fault type", list(FAULT_TYPES.keys()))
        delay_ms = st.slider("Added latency (ms, latency injection only)", min_value=50, max_value=3000,
                             value=500, step=50)
        objective = st.text_input("Experiment objective", "Observe service degradation and recovery")
        acknowledged = st.checkbox("I understand this experiment may affect availability.")
        submit_experiment = st.form_submit_button("Start experiment", type="primary", width="stretch")

    if submit_experiment:
        if not acknowledged:
            st.warning("Confirm the acknowledgement first.")
        elif API:
            payload = {"target_service": target, "fault_type": FAULT_TYPES[fault_label],
                       "hold": True, "objective": objective}
            if FAULT_TYPES[fault_label] == "latency":
                payload["delay_ms"] = delay_ms
            result = run_api_action("Start experiment", lambda: api_post("/experiments", payload))
            if result is not None:
                st.info("Queued. The controller first checks the app is healthy (~18 s), then injects the "
                        "fault. It stays active until you press Rollback (or the kill switch freezes it).")
        else:
            experiment_id = f"DEMO-{uuid.uuid4().hex[:8].upper()}"
            st.session_state.experiment_history.insert(0, {
                "id": experiment_id, "timestamp": now_iso(), "target": target, "fault": fault_label,
                "objective": objective, "status": "SIMULATED"})
            st.session_state.experiment_id = experiment_id
            st.session_state.experiment_status = "SIMULATED"
            st.session_state.rollback_verified = False
            log_event(f"Simulated experiment requested for {target}: {fault_label}.", "EXPERIMENT")
            st.info("Experiment recorded in demo mode. No real fault was injected.")

with right:
    st.markdown("#### Experiment status")

    if API:
        @st.fragment(run_every=LIVE_REFRESH)
        def live_experiment_status():
            s = safe_get("/status") or {}
            st.metric("Current state", s.get("experiment_status", "UNKNOWN"))
            cur = s.get("current_experiment")
            if cur:
                extra = f" (+{cur['params']['delay_ms']} ms)" if (cur.get("params") or {}).get("delay_ms") else ""
                st.write(f"**{cur.get('failure_type')}** on **{cur.get('target')}**{extra}")
                st.caption(f"Run `{cur['run_id']}`")
            faults = s.get("active_faults") or []
            if faults:
                st.error("Faults in place right now:\n\n" + "\n".join(
                    f"- **{f['fault']}** on **{f['service']}** ({f['detail']})" for f in faults)
                    + "\n\nPress **Rollback** (section 6) to restore the initial state.")
            elif not cur:
                st.caption("No fault is active - the system is in its initial state.")

        live_experiment_status()
    else:
        st.metric("Current state", st.session_state.experiment_status)
        st.write("Experiment ID:", st.session_state.experiment_id or "—")

    st.markdown("#### Kill switch")
    st.caption("Stops the agent immediately and freezes the system as it is. Nothing is cleaned up "
               "until you press Rollback.")
    if st.button("⛔ Kill switch (freeze current state)", width="stretch"):
        if API:
            run_api_action("Kill switch", lambda: api_post("/experiment/stop"))
        else:
            st.session_state.experiment_status = "STOPPED"
            st.session_state.random_injection_active = False
            log_event("Demo experiment marked stopped; no live fault existed.", "EXPERIMENT")
            st.success("Demo experiment stopped.")

# =========================================================
# 3. AI AGENT (API mode) / RANDOM ERROR INJECTION (demo mode)
# =========================================================

st.divider()

if API:
    st.subheader("3. AI agent: choose and inject errors")
    st.caption(
        "The AI reads the dependency map, the healthy baseline and every past result, ranks the most "
        "informative experiments with a reason for each, and the safety controller validates every proposal "
        "before it runs. The AI never runs commands itself. Its top-ranked fault stays active until you "
        "press Rollback."
    )

    a1, a2, a3 = st.columns([1, 1, 1])
    with a1:
        run_agent = st.button("🤖 Let AI choose & inject errors", type="primary", width="stretch")
    with a2:
        plan_only = st.button("🧠 Plan only (inject nothing)", width="stretch")
    with a3:
        stop_agent = st.button("⛔ Kill switch (freeze)", width="stretch", key="agent_kill")

    if run_agent:
        run_api_action("AI agent", lambda: api_post("/agent/run", {"execute": True, "hold": True}))
    if plan_only:
        run_api_action("AI planning", lambda: api_post("/agent/run", {"execute": False}))
    if stop_agent:
        run_api_action("Kill switch", lambda: api_post("/experiment/stop"))

    @st.fragment(run_every=LIVE_REFRESH)
    def live_agent():
        a = safe_get("/agent") or {}
        jobinfo = a.get("job") or {}
        if jobinfo.get("kind") == "agent":
            st.write(f"Agent job `{jobinfo.get('id')}`: **{jobinfo.get('status')}**")
            if jobinfo.get("error"):
                st.error(jobinfo["error"])
        if a.get("thinking") and jobinfo.get("status") == "PLANNING":
            with st.expander("AI reasoning (live)", expanded=True):
                st.text(a["thinking"][-1500:])
        plan = a.get("plan")
        if plan:
            st.markdown(f"**Latest AI plan** `{plan['plan_id']}` · model `{plan['model']}` · "
                        f"status `{plan['status']}`")
            if plan["status"] != "ok":
                st.error(f"Model unavailable: {plan.get('error')}. Nothing was executed.")
            elif plan["proposals"]:
                st.dataframe(pd.DataFrame([{
                    "rank": p["rank"], "target": p["target"], "fault": p["failure_type"],
                    "delay_ms": (p.get("params") or {}).get("delay_ms"),
                    "safety check": "✅ accepted" if p["verdict"] == "accepted" else f"❌ {p.get('rejection')}",
                    "why (AI)": p["reason"], "expected (AI)": p["hypothesis"],
                } for p in plan["proposals"]]), width="stretch", hide_index=True)

    live_agent()

else:
    st.subheader("3. Random error injection")
    st.caption("Generate simulated errors to test the dashboard. Demo mode does not cause actual failures.")

    inject_col, stop_random_col, counter_col = st.columns(3)
    with inject_col:
        inject_random = st.button(" ▶ Inject random errors", type="primary", width="stretch")
    with stop_random_col:
        stop_random = st.button("⏹ Stop random injection", width="stretch")
    with counter_col:
        st.metric("Generated demo errors", st.session_state.random_injection_count)

    if inject_random:
        if not st.session_state.ingestion_running:
            st.warning("Start ingestion first. Random demo events can only be generated while ingestion is running.")
        else:
            new_errors = []
            for service in random.sample(SERVICES, k=random.randint(1, 3)):
                error_type = random.choice(ERROR_TYPES)
                new_errors.append({"timestamp": now_iso(), "service": service, "type": error_type["type"],
                                   "message": error_type["message"], "suspected_cause": "Random demo injection",
                                   "source": "SIMULATION"})
            st.session_state.active_errors.extend(new_errors)
            st.session_state.error_history = (new_errors + st.session_state.error_history)[:200]
            st.session_state.random_injection_count += len(new_errors)
            st.session_state.random_injection_active = True
            st.session_state.rollback_verified = False
            st.session_state.current_latency = round(st.session_state.baseline_latency + random.uniform(20, 300), 2)
            st.session_state.current_error_rate = round(
                min(100.0, st.session_state.baseline_error_rate + random.uniform(1, 15)), 2)
            record_metrics()
            for error in new_errors:
                log_event(f"{error['type']} simulated for {error['service']}.", "ERROR")
            st.success(f"Generated {len(new_errors)} simulated error(s).")
            st.dataframe(pd.DataFrame(new_errors), width="stretch", hide_index=True)

    if stop_random:
        st.session_state.random_injection_active = False
        log_event("Random demo error generation stopped; event history retained.")
        st.success("Random generation stopped. Previous error events remain in the history.")

# =========================================================
# 4. METRICS, CHARTS, AFFECTED SERVICES
# =========================================================

st.divider()
st.subheader("4. System health and impact")


def render_live_health():
    m = safe_get("/metrics") or {}
    e = safe_get("/errors") or {}
    rows = m.get("rows") or []
    latest = next((r for r in reversed(rows) if r.get("n")), None)
    base = m.get("baseline") or {}

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active errors", sum(x.get("count", 1) for x in e.get("active", [])))
    m2.metric("Affected services", len(e.get("affected_services", [])))
    if latest:
        m3.metric("p95 latency", f"{latest['p95_ms']:.1f} ms",
                  delta=f"{latest['p95_ms'] - base.get('p95_ms', 0):+.1f} ms vs baseline", delta_color="inverse")
        m4.metric("Error rate", pct(latest["error_rate"]),
                  delta=f"{100 * (latest['error_rate'] - base.get('error_rate', 0)):+.1f} pts vs baseline",
                  delta_color="inverse")
    else:
        m3.metric("p95 latency", "—")
        m4.metric("Error rate", "—")
    if rows and latest and latest.get("experiment"):
        st.warning(f"Fault active: {latest['experiment']}")
    if not rows:
        st.info("No telemetry yet - press ▶ Start ingestion.")
        return

    df = pd.DataFrame(rows).set_index("timestamp")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("#### Response latency (p95, ms)")
        lat = df[["p95_ms"]].rename(columns={"p95_ms": "p95 latency"})
        lat["baseline"] = base.get("p95_ms")
        st.line_chart(lat)
    with c2:
        st.markdown("#### Error rate by user journey (%)")
        er = (df[["browse_error_rate", "cart_error_rate", "checkout_error_rate"]] * 100).rename(
            columns=lambda c: c.replace("_error_rate", ""))
        st.line_chart(er)

    s1, s2 = st.columns(2, gap="large")
    with s1:
        st.markdown("#### Affected services")
        active = e.get("active", [])
        if active:
            agg = pd.DataFrame(active).groupby("service").agg(
                errors=("count", "sum"), types=("type", lambda t: ", ".join(sorted(set(t))))).reset_index()
            st.dataframe(agg, width="stretch", hide_index=True)
        else:
            st.success("No active errors.")
    with s2:
        st.markdown("#### Active error details")
        if active:
            st.dataframe(pd.DataFrame(active)[["timestamp", "service", "type", "count", "attributed_by",
                                              "message", "suspected_cause"]],
                         width="stretch", hide_index=True)
        else:
            st.success("No active errors.")
    st.caption(f"Live measurements from synthetic shopper traffic, refreshed every {LIVE_REFRESH}. "
               f"Services are attributed from the shop's own error messages.")


if API:
    st.fragment(run_every=LIVE_REFRESH)(render_live_health)()
else:
    error_count = len(st.session_state.active_errors)
    affected_services = sorted({error["service"] for error in st.session_state.active_errors})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active errors", error_count)
    m2.metric("Affected services", len(affected_services))
    m3.metric("Latency", f"{st.session_state.current_latency:.1f} ms")
    m4.metric("Error rate", f"{st.session_state.current_error_rate:.2f}%")
    st.caption("All metrics above are simulated. They are not measurements from the live application.")

    chart_col, error_chart_col = st.columns(2, gap="large")
    with chart_col:
        st.markdown("#### Response latency")
        if st.session_state.metric_history:
            st.line_chart(pd.DataFrame(st.session_state.metric_history).set_index("timestamp")[["latency_ms"]])
        else:
            st.info("No latency samples available yet.")
    with error_chart_col:
        st.markdown("#### Error-rate trend")
        if st.session_state.metric_history:
            st.line_chart(pd.DataFrame(st.session_state.metric_history).set_index("timestamp")[["error_rate_pct"]])
        else:
            st.info("No error-rate samples available yet.")

    service_col, errors_col = st.columns(2, gap="large")
    with service_col:
        st.markdown("#### Affected services")
        if affected_services:
            st.dataframe(pd.DataFrame([{
                "service": s, "status": "SIMULATED ERROR",
                "active_errors": sum(e["service"] == s for e in st.session_state.active_errors)}
                for s in affected_services]), width="stretch", hide_index=True)
        else:
            st.success("No active simulated errors.")
    with errors_col:
        st.markdown("#### Active error details")
        if st.session_state.active_errors:
            st.dataframe(pd.DataFrame(st.session_state.active_errors), width="stretch", hide_index=True)
        else:
            st.success("No active simulated errors.")

# =========================================================
# 5. EVENT AND EXPERIMENT HISTORY
# =========================================================

st.divider()
st.subheader("5. Event and experiment history")


def render_history():
    history_tab, error_history_tab, exp_tab, services_tab = st.tabs(
        ["Event timeline", "All errors", "Experiment history", "Service health"])
    if API:
        events = safe_get("/events") or []
        errors = (safe_get("/errors") or {}).get("history", [])
        experiments = safe_get("/experiments/history") or []
        services = safe_get("/services", timeout=15) or []
    else:
        events = st.session_state.events
        errors = st.session_state.error_history
        experiments = st.session_state.experiment_history
        services = []
    with history_tab:
        if events:
            st.dataframe(pd.DataFrame(events), width="stretch", hide_index=True)
        else:
            st.caption("No events recorded yet.")
    with error_history_tab:
        if errors:
            df = pd.DataFrame(errors)
            cols = [c for c in ("timestamp", "service", "type", "count", "attributed_by", "message",
                                "suspected_cause", "source")
                    if c in df.columns]
            st.dataframe(df[cols], width="stretch", hide_index=True)
        else:
            st.caption("No errors recorded yet.")
    with exp_tab:
        if experiments:
            st.dataframe(pd.DataFrame(experiments), width="stretch", hide_index=True)
        else:
            st.caption("No experiments recorded yet.")
    with services_tab:
        if services:
            st.dataframe(pd.DataFrame(services), width="stretch", hide_index=True)
        else:
            st.caption("Service health is available in API mode.")


if API:
    st.fragment(run_every="5s")(render_history)()
else:
    render_history()

# =========================================================
# 6. ROLLBACK
# =========================================================

st.divider()
st.subheader("6. ↩ Roll back to initial state")

st.warning(
    "Rollback stops the agent, returns the system to its initial state (restores every service's replica "
    "count, removes any injected network delay, ends any crash loop), then verifies recovery using live "
    "telemetry. Telemetry and event history are retained."
)

rollback_confirmed = st.checkbox("I confirm rollback of the current experiment.")

if st.button("↩ ROLLBACK TO INITIAL STATE", type="primary", width="stretch",
             disabled=not rollback_confirmed):
    if API:
        with st.spinner("Rolling back and verifying recovery (can take 1-3 minutes while callers reconnect)..."):
            result = run_api_action("Rollback", lambda: api_post("/rollback", {"confirm": True}, timeout=300))
        if result is not None:
            st.session_state.last_rollback = result
    else:
        st.session_state.active_errors = []
        st.session_state.random_injection_active = False
        st.session_state.experiment_status = "ROLLED BACK"
        st.session_state.current_latency = st.session_state.baseline_latency
        st.session_state.current_error_rate = st.session_state.baseline_error_rate
        record_metrics()
        log_event("Demo rollback completed. Simulated active errors cleared; history preserved.", "RECOVERY")
        st.session_state.rollback_verified = False
        st.success("Demo rollback simulated: active errors cleared and baseline demo metrics restored.")
        st.info("This is not a real recovery verification because Demo mode does not inspect Kubernetes.")

if API and st.session_state.last_rollback:
    r = st.session_state.last_rollback
    if r.get("recovery_verified"):
        st.success(f"Backend verified recovery {r.get('recovery_time_s')} s after the restore: all services "
                   f"ready and live error rate / latency back within healthy limits.")
    else:
        st.warning("Rollback finished but recovery is NOT verified. "
                   + (f"Not ready: {', '.join(r['not_ready'])}. " if r.get("not_ready") else "")
                   + (r.get("note") or f"Health: {r.get('health')}"))
    if r.get("actions"):
        st.write("Changes reverted:", r["actions"])
    else:
        st.caption("Nothing needed reverting - the controller had already cleaned up.")

# =========================================================
# FOOTER
# =========================================================

st.divider()
st.caption(
    "ResilienceAI · Bosch Hackathon Prototype · API mode drives real, bounded faults in the local kind "
    "sandbox only; Demo mode is simulated."
)
