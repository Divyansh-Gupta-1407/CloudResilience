"""CloudWatch: chaos telemetry in, infrastructure metrics and alarm states out.

Publishing   every 3-second telemetry window (error rate, p50/p95 latency, per-journey error rates,
             active faults) becomes custom metrics in CR_METRIC_NAMESPACE, so experiments are visible
             in CloudWatch dashboards and can trigger CloudWatch alarms -> SNS.
Reading      real EC2 metrics (CPU, network, status checks) through GetMetricData, the published
             chaos metrics read back from CloudWatch, and the state of the project's alarms.

    python -m cloudresilience.aws.cloudwatch_monitor ec2 i-0abc... [--minutes 60]
    python -m cloudresilience.aws.cloudwatch_monitor telemetry [--minutes 30]
    python -m cloudresilience.aws.cloudwatch_monitor alarms
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from . import CloudError, call, client

APPLICATION = "online-boutique"
PUT_BATCH = 500                 # PutMetricData accepts up to 1000 datums / 1 MB per call; stay well inside

# telemetry row field -> (metric name, unit, scale)
TELEMETRY_METRICS = {
    "n": ("Requests", "Count", 1),
    "error_rate": ("ErrorRate", "Percent", 100),
    "p50_ms": ("LatencyP50", "Milliseconds", 1),
    "p95_ms": ("LatencyP95", "Milliseconds", 1),
    "browse_error_rate": ("BrowseErrorRate", "Percent", 100),
    "cart_error_rate": ("CartErrorRate", "Percent", 100),
    "checkout_error_rate": ("CheckoutErrorRate", "Percent", 100),
}

EC2_METRICS = [("CPUUtilization", "Average"), ("NetworkIn", "Sum"), ("NetworkOut", "Sum"),
               ("StatusCheckFailed", "Maximum")]


def dimensions(environment):
    return [{"Name": "Application", "Value": APPLICATION}, {"Name": "Environment", "Value": environment}]


def telemetry_datums(row, environment):
    """MetricDatum list for one telemetry window from chaos/telemetry.py. Missing values are skipped
    (an empty window has no latency), never sent as zero."""
    ts = datetime.fromtimestamp(row["t"], timezone.utc)
    dims = dimensions(environment)
    out = []
    for field, (name, unit, scale) in TELEMETRY_METRICS.items():
        v = row.get(field)
        if v is None:
            continue
        out.append({"MetricName": name, "Dimensions": dims, "Timestamp": ts, "Value": float(v) * scale, "Unit": unit})
    out.append({"MetricName": "ActiveFaults", "Dimensions": dims, "Timestamp": ts,
                "Value": float(len(row.get("faults") or [])), "Unit": "Count"})
    out.append({"MetricName": "Stalled", "Dimensions": dims, "Timestamp": ts,
                "Value": 1.0 if row.get("stalled") else 0.0, "Unit": "Count"})
    return out


def put_datums(datums, namespace, region=None, session=None):
    cw = client("cloudwatch", region, session)
    for i in range(0, len(datums), PUT_BATCH):
        call("cloudwatch:PutMetricData", cw.put_metric_data, Namespace=namespace, MetricData=datums[i:i + PUT_BATCH])
    return len(datums)


def _series(results):
    out = {}
    for r in results:
        pts = sorted(zip(r["Timestamps"], r["Values"]))
        out[r["Label"]] = [{"t": t.isoformat(), "value": round(v, 3)} for t, v in pts]
    return out


def _get_metric_data(queries, minutes, period, region=None, session=None):
    # End on the next period boundary so the current, still-filling period is included.
    now = time.time()
    end = datetime.fromtimestamp(now - now % period + period, timezone.utc)
    start = end - timedelta(minutes=minutes)
    cw = client("cloudwatch", region, session)
    pages = cw.get_paginator("get_metric_data").paginate(MetricDataQueries=queries, StartTime=start, EndTime=end,
                                                        ScanBy="TimestampAscending")
    results = {}
    for page in call("cloudwatch:GetMetricData", list, pages):
        for r in page["MetricDataResults"]:
            acc = results.setdefault(r["Id"], {"Label": r.get("Label") or r["Id"], "Timestamps": [], "Values": []})
            acc["Timestamps"] += r["Timestamps"]
            acc["Values"] += r["Values"]
    return _series(results.values())


def ec2_metrics(instance_id, minutes=60, period=300, region=None, session=None):
    """CPU, network and status-check series for one instance (standard 5-minute EC2 monitoring)."""
    if not instance_id.startswith("i-"):
        raise CloudError(f"not an EC2 instance id: {instance_id!r}", "InvalidInstanceId")
    queries = [{"Id": f"m{i}", "Label": name,
                "MetricStat": {"Metric": {"Namespace": "AWS/EC2", "MetricName": name,
                                          "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                               "Period": period, "Stat": stat}}
               for i, (name, stat) in enumerate(EC2_METRICS)]
    return {"instance_id": instance_id, "period_s": period, "minutes": minutes,
            "series": _get_metric_data(queries, minutes, period, region, session)}


def telemetry_metrics(namespace, environment, minutes=30, period=60, region=None, session=None):
    """The chaos telemetry as CloudWatch stores it (round trip of what the publisher sent)."""
    dims = dimensions(environment)
    wanted = [("Requests", "Sum"), ("ErrorRate", "Average"), ("LatencyP95", "Maximum"), ("ActiveFaults", "Maximum")]
    queries = [{"Id": f"t{i}", "Label": name,
                "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": name, "Dimensions": dims},
                               "Period": period, "Stat": stat}}
               for i, (name, stat) in enumerate(wanted)]
    return {"namespace": namespace, "environment": environment, "period_s": period, "minutes": minutes,
            "series": _get_metric_data(queries, minutes, period, region, session)}


def alarms(prefix="CloudResilience", region=None, session=None):
    cw = client("cloudwatch", region, session)
    pages = cw.get_paginator("describe_alarms").paginate(AlarmNamePrefix=prefix, AlarmTypes=["MetricAlarm"])
    out = []
    for page in call("cloudwatch:DescribeAlarms", list, pages):
        for a in page["MetricAlarms"]:
            out.append({"name": a["AlarmName"], "state": a["StateValue"], "metric": a.get("MetricName"),
                        "namespace": a.get("Namespace"), "threshold": a.get("Threshold"),
                        "comparison": a.get("ComparisonOperator"), "reason": a.get("StateReason"),
                        "updated": a["StateUpdatedTimestamp"].isoformat() if a.get("StateUpdatedTimestamp") else None,
                        "actions": a.get("AlarmActions", [])})
    return out


class TelemetryPublisher:
    """Streams new telemetry windows to CloudWatch in the background.

    `rows` is a callable returning the ingestor's current windows (oldest first). Every `interval_s`
    the windows not yet sent are published in one batched call. A failure is recorded and retried on
    the next tick; it never touches the telemetry itself."""

    def __init__(self, rows, namespace, environment, region=None, interval_s=15, session=None):
        self._rows, self.namespace, self.environment = rows, namespace, environment
        self.region, self.interval_s, self._session = region, interval_s, session
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.last_t = time.time()           # only windows recorded after start are published
        self.stats = {"windows_published": 0, "datums_published": 0, "last_publish": None, "last_error": None}

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="cloudwatch-publisher", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def publish_pending(self):
        """Send every window newer than the last one sent. Returns the number of windows published."""
        pending = [r for r in list(self._rows()) if r["t"] > self.last_t]
        if not pending:
            return 0
        datums = [d for r in pending for d in telemetry_datums(r, self.environment)]
        put_datums(datums, self.namespace, self.region, self._session)
        self.last_t = pending[-1]["t"]
        self.stats.update(windows_published=self.stats["windows_published"] + len(pending),
                          datums_published=self.stats["datums_published"] + len(datums),
                          last_publish=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_error=None)
        return len(pending)

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.publish_pending()
            except Exception as e:  # noqa: BLE001 - keep publishing on the next tick
                self.stats["last_error"] = str(e)

    def status(self):
        return {"running": self.running, "namespace": self.namespace, "environment": self.environment,
                "interval_s": self.interval_s, **self.stats}


def _cli():
    import argparse
    import json

    from ..config import CloudConfig
    cfg = CloudConfig.from_env()
    ap = argparse.ArgumentParser(prog="python -m cloudresilience.aws.cloudwatch_monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ec2", help="CPU / network / status checks for one instance")
    p.add_argument("instance_id")
    p.add_argument("--minutes", type=int, default=60)
    p = sub.add_parser("telemetry", help="chaos telemetry read back from CloudWatch")
    p.add_argument("--minutes", type=int, default=30)
    sub.add_parser("alarms", help="state of the CloudResilience alarms")
    a = ap.parse_args()
    if a.cmd == "ec2":
        out = ec2_metrics(a.instance_id, a.minutes, region=cfg.region)
    elif a.cmd == "telemetry":
        out = telemetry_metrics(cfg.metric_namespace, cfg.environment, a.minutes, region=cfg.region)
    else:
        out = alarms(region=cfg.region)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli()
