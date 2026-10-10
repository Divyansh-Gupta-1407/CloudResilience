"""Safety controller: validate -> steady-state check -> inject -> monitor -> cleanup -> recovery.

Stop paths (any one ends the fault immediately, cleanup always runs):
  threshold breach | manual kill switch (file or Ctrl+C) | duration cap | telemetry loss | error
"""
import json
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from .faults import CATALOG, Kube
from .probe import Probe, stats

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
PROXY_PORT = 8001


class ValidationError(ValueError):
    pass


class _Refused(Exception):
    pass


def load_safety():
    return yaml.safe_load((Path(__file__).with_name("safety.yaml")).read_text())


def validate(exp, cfg):
    """Deterministic gate. Raises ValidationError; nothing is injected unless this passes."""
    if not isinstance(exp, dict):
        raise ValidationError("experiment must be an object")
    ftype, target, duration = exp.get("failure_type"), exp.get("target"), exp.get("duration_s")
    hold = exp.get("hold") is True
    cap = cfg["hold_max_duration_s"] if hold else cfg["max_fault_duration_s"]
    if hold and duration is None:
        duration = cap   # held faults end on rollback / kill switch; the cap is only a backstop
    if ftype not in CATALOG:
        raise ValidationError(f"unsupported failure_type {ftype!r}; allowed: {sorted(CATALOG)}")
    if not isinstance(target, str) or target not in cfg["allowed_targets"]:
        raise ValidationError(f"target {target!r} is not in the allowlist")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        raise ValidationError(f"duration_s must be a positive number, got {duration!r}")
    if duration > cap:
        raise ValidationError(f"duration_s {duration} exceeds cap {cap}")
    schema = CATALOG[ftype].params_schema
    given = exp.get("params") or {}
    if not isinstance(given, dict):
        raise ValidationError("params must be an object")
    unknown = set(given) - set(schema)
    if unknown:
        raise ValidationError(f"unknown params for {ftype}: {sorted(unknown)}")
    params = {}
    for name, (lo, hi, default) in schema.items():
        v = given.get(name, default)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValidationError(f"{name} must be an integer between {lo} and {hi}, got {v!r}")
        params[name] = v
    return {"failure_type": ftype, "target": target, "duration_s": float(duration), "params": params,
            "hold": hold,
            "hypothesis": str(exp.get("hypothesis", ""))[:500], "reason": str(exp.get("reason", ""))[:500]}


class Proxy:
    """`kubectl proxy` so the probe reaches the frontend Service regardless of which pod serves it."""

    def __init__(self, context, namespace, port=PROXY_PORT):
        self.proc = subprocess.Popen(
            ["kubectl", "--context", context, "proxy", f"--port={port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = (f"http://127.0.0.1:{port}/api/v1/namespaces/{namespace}"
                     f"/services/frontend:80/proxy")
        time.sleep(2)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        self.proc.terminate()


# Observers (e.g. the dashboard API) register callables here to receive every controller
# event as it happens: hook(run_id, event_dict).
EVENT_HOOKS = []
# The experiment currently in progress (None when idle); read-only for observers.
# "squad": the multi-agent faults being held right now ([{agent, target, failure_type}] or None).
CURRENT = {"run": None, "squad": None}


def _log(events, msg, **kw):
    e = {"t": time.time(), "event": msg, **kw}
    events.append(e)
    print(f"[{time.strftime('%H:%M:%S')}] {msg} {kw if kw else ''}", flush=True)
    run = CURRENT["run"]
    for hook in EVENT_HOOKS:
        try:
            hook(run["run_id"] if run else None, e)
        except Exception:  # noqa: BLE001 - an observer must never break an experiment
            pass


def kill_switch_path(cfg):
    return ROOT / cfg["kill_switch_file"]


def capture_baseline(seconds=60):
    cfg = load_safety()
    proxy = Proxy(cfg["cluster_context"], cfg["namespace"])
    probe = Probe(proxy.base)
    try:
        probe.start()
        time.sleep(5)  # warm-up, not counted
        t0 = time.time()
        time.sleep(seconds)
        t1 = time.time()
        probe.stop()
    finally:
        proxy.close()
    samples = probe.window(t0, t1)
    by_journey = {j: stats([s for s in samples if s.journey == j]) for j in ("browse", "cart", "checkout")}
    baseline = {"captured_at": t0, "window_s": round(t1 - t0, 1), "overall": stats(samples),
                "by_journey": by_journey,
                "definitions": {"failure": "HTTP status not in (200, 302), connection error, or >5s timeout",
                                "p95_ms": "95th percentile of per-request latency in the window"}}
    OUT.mkdir(exist_ok=True)
    (OUT / "baseline.json").write_text(json.dumps(baseline, indent=2))
    (OUT / "baseline_samples.json").write_text(json.dumps(probe.dump(t0, t1)))
    return baseline


def run_experiment(exp_in, steady_state_s=15):
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    CURRENT["run"] = {"run_id": run_id, "request": exp_in, "started": time.time()}
    try:
        return _run_experiment(exp_in, steady_state_s, run_id)
    finally:
        CURRENT["run"] = None


def _run_experiment(exp_in, steady_state_s, run_id):
    cfg = load_safety()
    events = []
    record = {"run_id": run_id, "request": exp_in, "events": events}
    runs_dir = OUT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    def save():
        (runs_dir / f"{run_id}.json").write_text(json.dumps(record, indent=2, default=str))

    # 1. Validate before touching anything.
    try:
        exp = validate(exp_in, cfg)
    except ValidationError as e:
        record.update(status="rejected", reason=str(e))
        _log(events, "REJECTED", reason=str(e))
        save()
        return record
    record["experiment"] = exp
    CURRENT["run"]["experiment"] = exp

    if kill_switch_path(cfg).exists():
        record.update(status="refused", reason="kill switch file present; run `python -m chaos.run clear`")
        _log(events, "REFUSED", reason=record["reason"])
        save()
        return record

    baseline_file = OUT / "baseline.json"
    if not baseline_file.exists():
        record.update(status="refused", reason="no baseline; run `python -m chaos.run baseline` first")
        _log(events, "REFUSED", reason=record["reason"])
        save()
        return record
    baseline = json.loads(baseline_file.read_text())
    base_p95 = baseline["overall"]["p95_ms"]
    abort_p95 = max(base_p95 * cfg["abort_p95_multiplier"], cfg["abort_p95_floor_ms"])
    healthy_p95 = max(base_p95 * cfg["recovery_p95_multiplier"], cfg["recovery_p95_floor_ms"])
    record["baseline"] = baseline["overall"]
    record["limits"] = {**{k: cfg[k] for k in ("max_fault_duration_s", "window_s", "breach_windows",
                                               "abort_error_rate", "recovery_error_rate",
                                               "recovery_timeout_s", "recovery_windows")},
                        "abort_p95_ms": round(abort_p95, 1), "healthy_p95_ms": round(healthy_p95, 1)}
    record["app_revision"] = _app_revision()

    kube = Kube(cfg["cluster_context"], cfg["namespace"])
    fault = CATALOG[exp["failure_type"]](kube, exp["target"], exp["params"])
    proxy = Proxy(cfg["cluster_context"], cfg["namespace"])
    probe = Probe(proxy.base)
    win = cfg["window_s"]
    stop_reason, applied_at, removed_at = None, None, None

    try:
        probe.start()

        # 2. Steady-state check: don't inject into an already-unhealthy app.
        _log(events, "checking steady state", seconds=steady_state_s)
        time.sleep(3)
        t_pre = time.time()
        time.sleep(steady_state_s)
        pre = stats(probe.window(t_pre, time.time()))
        record["pre_fault"] = pre
        _log(events, "steady-state", **pre)
        if pre["n"] == 0 or pre["error_rate"] > cfg["recovery_error_rate"] or \
                pre["p95_ms"] > healthy_p95:
            raise _Refused("app not healthy before injection")

        # 3. Inject.
        _log(events, "fault applying", failure_type=exp["failure_type"], target=exp["target"])
        applied_at = time.time()
        record["apply_result"] = fault.apply()
        _log(events, "fault applied", **record["apply_result"])

        # 4. Monitor until a stop condition fires.
        breaches = empty = 0
        breach_logged = False
        windows = []
        while True:
            time.sleep(win)
            now = time.time()
            w = stats(probe.window(now - win, now))
            windows.append({"t": now, **w})
            elapsed = now - applied_at

            stalled = probe.stalled(abort_p95, now)
            w["stalled"] = stalled
            if exp["hold"] and hasattr(fault, "sustain"):
                fault.sustain()   # e.g. keep a crashed service crashed (crash loop) until rollback
            if kill_switch_path(cfg).exists():
                stop_reason = "manual_kill_switch"
            elif not proxy.alive() or not probe.alive():
                stop_reason = "telemetry_loss"
            elif w["n"] == 0 and stalled == 0:
                empty += 1
                stop_reason = "telemetry_loss" if empty >= cfg["telemetry_loss_windows"] else None
            else:
                empty = 0
                over_err = w["n"] > 0 and w["error_rate"] > cfg["abort_error_rate"]
                # Requests stuck longer than the latency limit count as a latency breach.
                over_lat = (w["n"] > 0 and w["p95_ms"] > abort_p95) or stalled > 0
                breaches = breaches + 1 if (over_err or over_lat) else 0
                if breaches >= cfg["breach_windows"]:
                    if exp["hold"]:
                        # Hold mode: an operator asked to keep the fault until rollback / kill switch.
                        # Record the breach (once) instead of aborting.
                        if not breach_logged:
                            breach_logged = True
                            record["threshold_breached_at"] = now
                            _log(events, "THRESHOLD BREACH (holding until rollback or kill switch)",
                                 error_rate=w["error_rate"], p95_ms=w["p95_ms"], stalled_requests=stalled)
                    else:
                        stop_reason = "threshold_breach"
                        _log(events, "THRESHOLD BREACH", error_rate=w["error_rate"], p95_ms=w["p95_ms"],
                             stalled_requests=stalled, limit_err=cfg["abort_error_rate"],
                             limit_p95=round(abort_p95, 1))
            cap = cfg["hold_max_duration_s"] if exp["hold"] else cfg["max_fault_duration_s"]
            if not stop_reason and elapsed >= exp["duration_s"]:
                stop_reason = "hold_backstop_timeout" if exp["hold"] else "duration_complete"
            if not stop_reason and elapsed >= cap:
                stop_reason = "max_duration_cap"
            print(f"    t+{elapsed:5.1f}s  n={w['n']:<4} err={w['error_rate']}  p95={w['p95_ms']}ms"
                  f"  stalled={stalled}", flush=True)
            if stop_reason:
                break
        record["fault_windows"] = windows

    except _Refused as e:
        record.update(status="refused", reason=str(e))
        _log(events, "REFUSED", reason=str(e))
    except KeyboardInterrupt:
        stop_reason = "manual_ctrl_c"
    except Exception as e:  # noqa: BLE001 - any failure must still reach cleanup
        stop_reason = f"error: {e}"
        _log(events, "ERROR", error=str(e))
    finally:
        # 5. Cleanup runs on every stop path except the kill switch, which freezes the system in
        #    its current state (fault left in place) until an operator presses rollback.
        frozen = applied_at is not None and stop_reason == "manual_kill_switch"
        if frozen:
            record["frozen"] = True
            record["cleanup_status"] = "skipped: frozen by kill switch (rollback restores the initial state)"
            if exp["hold"] and hasattr(fault, "freeze"):
                try:
                    record["freeze_result"] = fault.freeze()
                except Exception as e:  # noqa: BLE001
                    record["freeze_result"] = f"FAILED: {e}"
            _log(events, "KILL SWITCH - process stopped, fault left in place until rollback",
                 failure_type=exp["failure_type"], target=exp["target"])
        elif applied_at is not None:
            try:
                record["cleanup_result"] = fault.cleanup()
                record["cleanup_status"] = "ok"
            except Exception as e:  # noqa: BLE001
                record["cleanup_status"] = f"FAILED: {e}"
            removed_at = time.time()
            _log(events, "fault removed", stop_reason=stop_reason, cleanup=record["cleanup_status"])

    try:
        if applied_at is not None:
            end = removed_at or time.time()
            record["during_fault"] = stats(probe.window(applied_at, end))
            if not frozen:
                record["recovery"] = _await_recovery(cfg, probe, kube, exp["target"], removed_at, healthy_p95,
                                                     events)
        status = ("frozen" if frozen else "completed") if applied_at else record.get("status",
                                                                                      "aborted_before_injection")
        record.update(status=status, stop_reason=stop_reason, applied_at=applied_at, removed_at=removed_at)
    finally:
        probe.stop()
        proxy.close()
        record["samples"] = probe.dump()
        save()
        print(f"run record: {runs_dir / (run_id + '.json')}")
    return record


def _await_recovery(cfg, probe, kube, target, removed_at, healthy_p95, events):
    win, need, deadline = cfg["window_s"], cfg["recovery_windows"], removed_at + cfg["recovery_timeout_s"]
    streak_start, streak = None, 0
    while time.time() < deadline:
        time.sleep(win)
        now = time.time()
        w = stats(probe.window(now - win, now))
        healthy = (w["n"] > 0 and w["error_rate"] <= cfg["recovery_error_rate"]
                   and w["p95_ms"] <= healthy_p95
                   and kube.ready(target) > 0)
        if healthy:
            streak_start = streak_start or (now - win)
            streak += 1
            if streak >= need:
                rt = round(max(0.0, streak_start - removed_at), 1)
                _log(events, "recovered", recovery_time_s=rt)
                return {"recovered": True, "recovery_time_s": rt}
        else:
            streak_start, streak = None, 0
    _log(events, "NOT RECOVERED", timeout_s=cfg["recovery_timeout_s"])
    return {"recovered": False, "recovery_time_s": None, "timeout_s": cfg["recovery_timeout_s"]}


def _app_revision():
    try:
        return subprocess.run(["git", "-C", str(ROOT / "microservices-demo"), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
