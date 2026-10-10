"""Synthetic user-journey probe: steady traffic + per-request measurements.

Requests go through `kubectl proxy` (API-server service proxy), which keeps
working while individual frontend pods are killed or restarted.
"""
import html
import random
import re
import threading
import time
from dataclasses import dataclass, asdict

import requests

PRODUCTS = ["0PUK6V6EV0", "1YMWWN1N4O", "2ZYFJ3GM2N", "66VCHSJNUP", "6E92ZMYYFZ",
            "9SIQT8TOJO", "L9ECAV7KIM", "LS4PSXUNUM", "OLJCESPC7Z"]
CHECKOUT_FORM = {
    "email": "chaos-probe@example.com", "street_address": "1 Test St", "zip_code": "94043",
    "city": "Mountain View", "state": "CA", "country": "United States",
    "credit_card_number": "4432801561520454", "credit_card_expiration_month": "1",
    "credit_card_expiration_year": "2039", "credit_card_cvv": "672",
}
REQUEST_TIMEOUT_S = 5.0
_ERROR_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S)


@dataclass
class Sample:
    t: float          # epoch seconds at request start
    journey: str
    step: str
    status: int       # HTTP status, 0 on connection error/timeout
    latency_ms: float
    ok: bool
    detail: str = ""  # error text from the shop's error page, or the client-side exception
    degraded: str = ""  # X-Degraded header: services the page had to do without (page still worked)


def error_detail(text):
    """The frontend's error page puts the underlying error in a <pre>: the failing call on the first
    line, what the page was doing on the second, then a Go stack trace (dropped)."""
    m = _ERROR_PRE.search(text or "")
    if not m:
        return ""
    lines = [ln.strip() for ln in html.unescape(m.group(1)).splitlines() if ln.strip()]
    return " | ".join(lines[:2])[:300]


class Probe:
    def __init__(self, base_url, workers=4, think_time_s=0.2):
        self.base = base_url.rstrip("/")
        self.workers = workers
        self.think = think_time_s
        self.samples: list[Sample] = []
        self._inflight: dict[int, float] = {}   # request id -> start time, for requests still waiting
        self._next_id = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    def _req(self, sess, journey, step, method, path, data=None):
        t0 = time.time()
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._inflight[rid] = t0
        detail = degraded = ""
        try:
            r = sess.request(method, self.base + path, data=data, timeout=REQUEST_TIMEOUT_S,
                             allow_redirects=False)
            status = r.status_code
            degraded = r.headers.get("X-Degraded", "")
            if status >= 400:
                detail = error_detail(r.text)
        except requests.Timeout:
            status, detail = 0, f"timeout after {REQUEST_TIMEOUT_S:.0f}s"
        except requests.RequestException as e:
            status, detail = 0, f"connection error: {type(e).__name__}"
        latency = (time.time() - t0) * 1000
        s = Sample(t0, journey, step, status, latency, status in (200, 302), detail, degraded)
        with self._lock:
            self.samples.append(s)
            del self._inflight[rid]
        return s.ok

    def stalled(self, older_than_ms, now=None):
        """Number of requests still waiting for longer than `older_than_ms`.

        Completed samples only appear when a request finishes, so a hung dependency
        would otherwise look like an empty window (telemetry loss) rather than slowness.
        """
        now = now or time.time()
        with self._lock:
            return sum(1 for t0 in self._inflight.values() if (now - t0) * 1000 > older_than_ms)

    def alive(self):
        return any(th.is_alive() for th in self._threads)

    def _journey(self, sess, name):
        p = random.choice(PRODUCTS)
        if name == "browse":
            self._req(sess, name, "home", "GET", "/") and \
                self._req(sess, name, "product", "GET", f"/product/{p}")
        elif name == "cart":
            self._req(sess, name, "add", "POST", "/cart", {"product_id": p, "quantity": "1"}) and \
                self._req(sess, name, "view", "GET", "/cart")
        elif name == "checkout":
            self._req(sess, name, "add", "POST", "/cart", {"product_id": p, "quantity": "1"}) and \
                self._req(sess, name, "place_order", "POST", "/cart/checkout", CHECKOUT_FORM)

    def _worker(self):
        sess = requests.Session()
        # Weighted mix roughly like the bundled loadgenerator: mostly browsing.
        mix = ["browse"] * 6 + ["cart"] * 3 + ["checkout"] * 1
        while not self._stop.is_set():
            self._journey(sess, random.choice(mix))
            self._stop.wait(self.think)

    def start(self):
        self._stop.clear()
        self._threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for th in self._threads:
            th.start()

    def stop(self):
        self._stop.set()
        for th in self._threads:
            th.join(timeout=REQUEST_TIMEOUT_S + 1)

    def window(self, t0, t1):
        with self._lock:
            return [s for s in self.samples if t0 <= s.t < t1]

    def dump(self, t0=None, t1=None):
        with self._lock:
            return [asdict(s) for s in self.samples
                    if (t0 is None or s.t >= t0) and (t1 is None or s.t < t1)]


def stats(samples):
    """Error rate and latency percentiles over a list of samples (None when empty)."""
    n = len(samples)
    if n == 0:
        return {"n": 0, "error_rate": None, "p50_ms": None, "p95_ms": None}
    lat = sorted(s.latency_ms for s in samples)
    pct = lambda q: lat[min(n - 1, int(q * n))]
    fails = sum(1 for s in samples if not s.ok)
    return {"n": n, "error_rate": round(fails / n, 4),
            "p50_ms": round(pct(0.50), 1), "p95_ms": round(pct(0.95), 1)}
