"""Approved fault templates. Each has apply() and cleanup(); nothing else executes.

All cluster calls go through `kubectl` (fixed context and namespace) or `docker exec`
on the kind node, built from validated fields only - never from free text.

params_schema maps each allowed parameter to (min, max, default); the controller
rejects anything else before a fault is constructed.
"""
import json
import re
import subprocess
import threading

_VETH = re.compile(r"^veth[0-9a-f]+$")


class Kube:
    def __init__(self, context, namespace):
        self.context = context
        self.namespace = namespace

    def run(self, *args, check=True):
        cmd = ["kubectl", "--context", self.context, "-n", self.namespace, *args]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr.strip()}")
        return p.stdout

    def replicas(self, deployment):
        d = json.loads(self.run("get", "deployment", deployment, "-o", "json"))
        return d["spec"].get("replicas", 1)

    def ready(self, deployment):
        d = json.loads(self.run("get", "deployment", deployment, "-o", "json"))
        return d["status"].get("readyReplicas", 0) or 0

    def pods(self, app):
        """[(pod name, pod IP, node name)] for running pods of one app."""
        items = json.loads(self.run("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]
        return [(p["metadata"]["name"], p["status"].get("podIP"), p["spec"].get("nodeName"))
                for p in items if p["status"].get("phase") == "Running" and p["status"].get("podIP")]


def _node_exec(node, *args, check=True):
    """Run a command inside a kind node container (kind node names are docker container names)."""
    p = subprocess.run(["docker", "exec", node, *args], capture_output=True, text=True, timeout=30)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} on {node} failed: {p.stderr.strip()}")
    return p.stdout


def _pod_veth(node, pod_ip):
    """Host-side veth of a pod: traffic leaving it goes *into* the pod."""
    route = _node_exec(node, "ip", "-o", "route", "get", pod_ip)
    m = re.search(r"\bdev (\S+)", route)
    if not m or not _VETH.match(m.group(1)):
        raise RuntimeError(f"could not find pod interface for {pod_ip}: {route.strip()}")
    return m.group(1)


class ServiceDown:
    """Service outage: scale the deployment to 0 replicas for the duration, then restore it."""
    name = "service-down"
    params_schema = {}

    def __init__(self, kube, target, params):
        self.kube, self.target, self.params = kube, target, params
        self.original_replicas = None

    def apply(self):
        self.original_replicas = self.kube.replicas(self.target)
        self.kube.run("scale", "deployment", self.target, "--replicas=0")
        return {"original_replicas": self.original_replicas}

    def cleanup(self):
        if self.original_replicas is None:   # apply never got far enough to change anything
            return {"restored": "nothing to restore"}
        self.kube.run("scale", "deployment", self.target, f"--replicas={self.original_replicas}")
        return {"restored_replicas": self.original_replicas}


class PodKill:
    """Container crash: force-delete one pod; Kubernetes recreates it (tests self-healing).
    When held, the replacement pods are killed too, every monitoring window (a crash loop),
    so the service stays down until rollback."""
    name = "pod-kill"
    params_schema = {}

    CRASH_LOOP_INTERVAL_S = 1.0   # restarted pods come back in ~1-2 s; check faster than that

    def __init__(self, kube, target, params):
        self.kube, self.target, self.params = kube, target, params
        self.kills = 0
        self._stop = threading.Event()
        self._loop = None

    def _kill(self, pod_names):
        for name in pod_names:
            self.kube.run("delete", "pod", name, "--grace-period=0", "--force", "--wait=false", check=False)
            self.kills += 1

    def apply(self):
        pods = self.kube.pods(self.target)
        if not pods:
            raise RuntimeError(f"no running pods for app={self.target}")
        self._kill([pods[0][0]])
        return {"killed_pod": pods[0][0]}

    def _kill_alive(self):
        items = json.loads(self.kube.run("get", "pods", "-l", f"app={self.target}", "-o", "json",
                                         check=False) or '{"items": []}')["items"]
        self._kill([p["metadata"]["name"] for p in items if not p["metadata"].get("deletionTimestamp")])

    def _crash_loop(self):
        while not self._stop.wait(self.CRASH_LOOP_INTERVAL_S):
            try:
                self._kill_alive()
            except Exception:  # noqa: BLE001 - keep looping; the controller still monitors
                pass

    def sustain(self):
        """Hold mode: keep the service crashed by killing every replacement pod, checked every second
        in a background loop (started on the first call) so the outage is continuous, not flickering."""
        if self._loop is None:
            self._loop = threading.Thread(target=self._crash_loop, daemon=True)
            self._loop.start()

    def _stop_loop(self):
        self._stop.set()
        if self._loop:
            self._loop.join(timeout=5)

    def freeze(self):
        """Kill switch: the crash loop stops with the process, so pin the service at 'crashed'
        (0 replicas) instead of letting Kubernetes quietly heal it. Rollback restores replicas."""
        self._stop_loop()
        self.kube.run("scale", "deployment", self.target, "--replicas=0")
        return {"frozen_as": "0 replicas (crashed)", "pods_killed": self.kills}

    def cleanup(self):
        self._stop_loop()
        return {"restored": "self-healing (ReplicaSet recreates the pod)", "pods_killed": self.kills}


class NetworkDelay:
    """Network latency: add a fixed delay (tc netem) to all traffic into the target's pods, then remove it.
    The service stays up but every call to it is slow - tests caller timeouts."""
    name = "network-delay"
    params_schema = {"delay_ms": (50, 3000, 500)}

    def __init__(self, kube, target, params):
        self.kube, self.target = kube, target
        self.delay_ms = int(params["delay_ms"])
        self.applied = []   # [(node, veth)]

    def apply(self):
        pods = self.kube.pods(self.target)
        if not pods:
            raise RuntimeError(f"no running pods for app={self.target}")
        for _, ip, node in pods:
            dev = _pod_veth(node, ip)
            _node_exec(node, "tc", "qdisc", "replace", "dev", dev, "root", "netem",
                       "delay", f"{self.delay_ms}ms")
            self.applied.append((node, dev))
        return {"delay_ms": self.delay_ms, "interfaces": [d for _, d in self.applied]}

    def cleanup(self):
        for node, dev in self.applied:
            _node_exec(node, "tc", "qdisc", "del", "dev", dev, "root", check=False)
        return {"removed_delay_from": [d for _, d in self.applied]}


CATALOG = {f.name: f for f in (ServiceDown, PodKill, NetworkDelay)}


def find_faults(kube, targets, desired_replicas):
    """Every difference from the initial state that our faults can leave behind:
    deployments scaled below their manifest replica count, and netem delays on pod interfaces.
    Returns [{"service", "fault", "detail", ...}] (empty list = initial state).

    Uses one kubectl call per resource type and two commands per node, so it is cheap enough
    to poll every few seconds."""
    found = []
    deploys = {d["metadata"]["name"]: d["spec"].get("replicas", 1)
               for d in json.loads(kube.run("get", "deployments", "-o", "json"))["items"]}
    for t in targets:
        want, have = desired_replicas.get(t, 1), deploys.get(t)
        if have is not None and have != want:
            found.append({"service": t, "fault": "service-down", "detail": f"{have}/{want} replicas",
                          "want": want})

    pods = json.loads(kube.run("get", "pods", "-o", "json"))["items"]
    ip_to_app, nodes = {}, set()
    for p in pods:
        app, ip, node = p["metadata"].get("labels", {}).get("app"), p["status"].get("podIP"), p["spec"].get("nodeName")
        if app in targets and ip and node:
            ip_to_app[ip] = app
            nodes.add(node)
    for node in nodes:
        routes = _node_exec(node, "ip", "-o", "route", "show", check=False)
        dev_to_app = {}
        for line in routes.splitlines():
            m = re.match(r"(\d+\.\d+\.\d+\.\d+) dev (veth[0-9a-f]+)", line)
            if m and m.group(1) in ip_to_app:
                dev_to_app[m.group(2)] = ip_to_app[m.group(1)]
        for line in _node_exec(node, "tc", "qdisc", "show", check=False).splitlines():
            m = re.search(r"qdisc netem \S+ dev (veth[0-9a-f]+) .*?delay (\S+)", line)
            if m and m.group(1) in dev_to_app:
                found.append({"service": dev_to_app[m.group(1)], "fault": "network-delay",
                              "detail": f"+{m.group(2)} on {m.group(1)}", "node": node, "dev": m.group(1)})
    return found


def reset_all(kube, targets, desired_replicas):
    """Rollback helper: undo everything find_faults() reports, returning to the manifest's initial state.
    Returns a list of actions taken."""
    actions = []
    for f in find_faults(kube, targets, desired_replicas):
        if f["fault"] == "service-down":
            kube.run("scale", "deployment", f["service"], f"--replicas={f['want']}")
            actions.append(f"scaled {f['service']} back to {f['want']}")
        elif f["fault"] == "network-delay":
            _node_exec(f["node"], "tc", "qdisc", "del", "dev", f["dev"], "root", check=False)
            actions.append(f"removed network delay ({f['detail']}) from {f['service']}")
    return actions
