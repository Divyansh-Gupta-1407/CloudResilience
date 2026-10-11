"""Live telemetry for the dashboard: continuous light probe traffic, 3-second metric windows,
and error events attributed to the service that most likely caused them.

Runs independently of experiments (its own kubectl proxy port), so the dashboard keeps
showing what users would see before, during and after every fault.
"""
import collections
import json
import re
import threading
import time
from datetime import datetime, timezone

from . import db
from .controller import CURRENT, Proxy, load_safety
from .probe import Probe, stats

WINDOW_S = 3
ACTIVE_ERROR_S = 15          # an error counts as "active" for this long after it was seen
SLOW_MS_FLOOR = 250          # a successful request slower than this is a "High latency" event
INGEST_PROXY_PORT = 8002     # experiments use 8001

# The shop's error page text names the failing call, not the service, so map phrases first.
_PHRASES = [
    (re.compile(r"charge|payment", re.I), "paymentservice"),
    (re.compile(r"ship", re.I), "shippingservice"),
    (re.compile(r"currenc", re.I), "currencyservice"),
    (re.compile(r"recommend", re.I), "recommendationservice"),
    (re.compile(r"\bads?\b|advert", re.I), "adservice"),
    (re.compile(r"redis", re.I), "redis-cart"),
    (re.compile(r"\bcart\b", re.I), "cartservice"),
    (re.compile(r"product", re.I), "productcatalogservice"),
    (re.compile(r"checkout|place ?order|complete the order", re.I), "checkoutservice"),
]
_PORTS = {"7070": "cartservice", "3550": "productcatalogservice", "7000": "currencyservice",
          "5050": "checkoutservice", "9555": "adservice", "6379": "redis-cart"}


# Which services each probed page needs (frontend handlers). Used to attribute an error that has no
# clue in its message (e.g. a slow page) when several faults are active at once.
STEP_DEPS = {
    "browse/home": {"currencyservice", "productcatalogservice", "cartservice", "redis-cart", "adservice"},
    "browse/product": {"productcatalogservice", "currencyservice", "cartservice", "redis-cart",
                       "recommendationservice", "adservice"},
    "cart/add": {"productcatalogservice", "cartservice", "redis-cart"},
    "checkout/add": {"productcatalogservice", "cartservice", "redis-cart"},
    "cart/view": {"cartservice", "redis-cart", "productcatalogservice", "currencyservice", "shippingservice",
                  "recommendationservice"},
    "checkout/place_order": {"checkoutservice", "cartservice", "redis-cart", "productcatalogservice",
                             "currencyservice", "shippingservice", "paymentservice", "emailservice"},
}


def _squad_guess(squad, sample, kind):
    """Best active squad fault for an error without a clue: one this page depends on, preferring
    the latency fault for slow/timeout errors."""
    deps = STEP_DEPS.get(f"{sample.journey}/{sample.step}", set())
    cands = [f for f in squad if f["target"] in deps]
    if not cands:
        return None
    if kind in ("High latency", "Timeout"):
        cands.sort(key=lambda f: f["failure_type"] != "network-delay")
    return cands[0]["target"], "only active fault on this page" if len(cands) == 1 else "active fault on this page"


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def attribute(detail):
    """Best guess of the service behind an error message; None if the text gives no clue."""
    for rx, svc in _PHRASES:
        if rx.search(detail):
            return svc
    for port, svc in _PORTS.items():
        if f":{port}" in detail:
            return svc
    return None


def classify(sample, slow_ms):
    if sample.status == 0:
        return "Timeout" if sample.detail.startswith("timeout") else "Connection error"
    if sample.status >= 500:
        if re.search(r"Unavailable|connection refused|no such host|DeadlineExceeded", sample.detail):
            return "Dependency error"
        return f"HTTP {sample.status}"
    if sample.status >= 400:
        return f"HTTP {sample.status}"
    if sample.latency_ms > slow_ms:
        return "High latency"
    return None


class Ingestor:
    def __init__(self):
        self.cfg = load_safety()
        self.running = False
        self.started_at = None
        self.metrics = collections.deque(maxlen=600)    # one row per window (~30 min)
        self.errors = collections.deque(maxlen=1000)    # aggregated error events, newest last
        self._lock = threading.Lock()
        self._proxy = self._probe = self._thread = None
        self._stop = threading.Event()
        baseline = db.latest_baseline()
        self.baseline = baseline["overall"] if baseline else None

    @property
    def slow_ms(self):
        base = (self.baseline or {}).get("p95_ms") or 25
        return max(base * self.cfg["abort_p95_multiplier"], SLOW_MS_FLOOR)

    def start(self):
        with self._lock:
            if self.running:
                return
            self._proxy = Proxy(self.cfg["cluster_context"], self.cfg["namespace"], port=INGEST_PROXY_PORT)
            self._probe = Probe(self._proxy.base, workers=2, think_time_s=0.5)
            self._probe.start()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self.running, self.started_at = True, time.time()

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self._stop.set()
            self._probe.stop()
            self._proxy.close()
            self.running = False

    def _loop(self):
        last = time.time()
        while not self._stop.wait(WINDOW_S):
            now = time.time()
            # Requests are recorded when they finish; read the window one step behind so slow
            # requests that started in it have had a chance to complete.
            samples = self._probe.window(last - WINDOW_S, now - WINDOW_S) if now - self.started_at > WINDOW_S else []
            last = now
            self._record_window(now, samples)
            # Keep the probe's sample list bounded; we only ever read recent windows.
            with self._probe._lock:
                cutoff = now - 60
                self._probe.samples = [s for s in self._probe.samples if s.t >= cutoff]

    def _record_window(self, now, samples):
        run = CURRENT["run"]
        exp = (run or {}).get("experiment") or {}
        squad = CURRENT.get("squad") or []
        overall = stats(samples)
        row = {"t": now, "timestamp": datetime.now().strftime("%H:%M:%S"), **overall,
               "stalled": self._probe.stalled(self.slow_ms, now),
               "experiment": f"{exp.get('failure_type')} on {exp.get('target')}" if exp else None,
               # every fault being held in this window (single experiment or squad), for chart shading
               "faults": ([f"{exp.get('failure_type')} on {exp.get('target')}"] if exp else [])
                         + [f"{f['failure_type']} on {f['target']}" for f in squad]}
        for j in ("browse", "cart", "checkout"):
            js = stats([s for s in samples if s.journey == j])
            row[f"{j}_error_rate"] = js["error_rate"]
            row[f"{j}_p95_ms"] = js["p95_ms"]
        self.metrics.append(row)
        self._persist(db.save_window, row)

        groups = collections.defaultdict(list)
        squad_targets = {f["target"] for f in squad} | ({exp["target"]} if exp else set())
        # The frontend only ever talks to cartservice, so a redis-cart outage surfaces as a cartservice
        # error; credit it to redis-cart when that is the one under test.
        behind = lambda svc: "redis-cart" if svc == "cartservice" and "redis-cart" in squad_targets \
            and "cartservice" not in squad_targets else svc
        for s in samples:
            # A page that worked but had to fall back (resilient frontend): count it against each
            # service it had to do without, separately from real failures.
            if s.ok and s.degraded:
                for svc in s.degraded.split(","):
                    groups[(behind(svc.strip()), "Degraded (fallback)", "degradation header")].append(s)
                continue
            kind = classify(s, self.slow_ms)
            if kind:
                svc = attribute(s.detail)
                svc = svc and behind(svc)
                how = "error message"
                if not svc and squad:
                    svc, how = _squad_guess(squad, s, kind) or (None, None)
                if not svc:
                    svc, how = (exp["target"], "active experiment") if exp else ("frontend", "entry point")
                groups[(svc, kind, how)].append(s)
        squad_cause = {f["target"]: f"{f['failure_type']} on {f['target']} (agent {f['agent']})" for f in squad}
        for (svc, kind, how), ss in groups.items():
            first = ss[0]
            self.errors.append({
                "t": now, "timestamp": _iso(now), "service": svc, "type": kind, "count": len(ss),
                "attributed_by": how,
                "message": (first.detail or (f"page worked without {svc} (fallback shown)" if kind.startswith("Degraded")
                                             else f"{first.step} took {first.latency_ms:.0f} ms")),
                "journeys": sorted({s.journey for s in ss}),
                "steps": sorted({f"{s.journey}/{s.step}" for s in ss}),
                "suspected_cause": (f"{exp['failure_type']} on {exp['target']} (run {run['run_id']})" if exp
                                    else squad_cause.get(svc, "squad: " + ", ".join(squad_cause.values()))
                                    if squad else "no experiment running"),
                "source": "LIVE",
            })
            self._persist(db.save_error, self.errors[-1])

    @staticmethod
    def _persist(fn, item):
        try:
            fn(item)
        except Exception:  # noqa: BLE001 - a database hiccup must never stop live telemetry
            pass

    def active_errors(self):
        cutoff = time.time() - ACTIVE_ERROR_S
        return [e for e in self.errors if e["t"] >= cutoff]

    def health(self):
        """Latest window vs. the baseline - used by rollback to verify recovery."""
        recent = [m for m in list(self.metrics)[-3:] if m["n"]]
        if not recent:
            return {"healthy": None, "reason": "no recent telemetry (is ingestion running?)"}
        err = max(m["error_rate"] for m in recent)
        p95 = max(m["p95_ms"] for m in recent)
        healthy_p95 = max((self.baseline or {}).get("p95_ms", 25) * self.cfg["recovery_p95_multiplier"],
                          self.cfg["recovery_p95_floor_ms"])
        ok = err <= self.cfg["recovery_error_rate"] and p95 <= healthy_p95
        return {"healthy": ok, "error_rate": err, "p95_ms": p95, "healthy_p95_ms": healthy_p95,
                "windows": len(recent)}
