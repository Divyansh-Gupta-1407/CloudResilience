import time

import boto3
import pytest

from cloudresilience.aws import CloudError, cloudwatch_monitor as cw
from .conftest import REGION

NS = "CloudResilience/Test"


def _row(t, n=40, err=0.25, p95=180.0, faults=("network-delay on cartservice",)):
    """Shaped like a chaos/telemetry.py window."""
    return {"t": t, "timestamp": "12:00:00", "n": n, "error_rate": err, "p50_ms": 30.0, "p95_ms": p95,
            "stalled": 0, "browse_error_rate": 0.0, "cart_error_rate": 0.5, "checkout_error_rate": None,
            "faults": list(faults), "experiment": None}


def test_telemetry_datums_scale_and_skip_missing():
    datums = {d["MetricName"]: d for d in cw.telemetry_datums(_row(time.time()), "sandbox")}
    assert datums["ErrorRate"]["Value"] == 25.0 and datums["ErrorRate"]["Unit"] == "Percent"
    assert datums["LatencyP95"]["Value"] == 180.0 and datums["LatencyP95"]["Unit"] == "Milliseconds"
    assert datums["Requests"]["Value"] == 40
    assert datums["ActiveFaults"]["Value"] == 1
    assert "CheckoutErrorRate" not in datums          # None in the window: not sent as a fake zero
    assert {"Name": "Environment", "Value": "sandbox"} in datums["ErrorRate"]["Dimensions"]


def test_empty_window_sends_no_latency():
    row = _row(time.time(), n=0, err=None, p95=None, faults=())
    names = {d["MetricName"] for d in cw.telemetry_datums(row, "sandbox")}
    assert "LatencyP95" not in names and "ErrorRate" not in names
    assert {"Requests", "ActiveFaults", "Stalled"} <= names


def test_publish_and_read_back_from_cloudwatch(aws):
    now = time.time()
    rows = [_row(now - 120 + 3 * i) for i in range(10)]
    sent = cw.put_datums([d for r in rows for d in cw.telemetry_datums(r, "sandbox")], NS, REGION)
    assert sent == 10 * 8      # checkout_error_rate is None in these windows

    listed = boto3.client("cloudwatch", region_name=REGION).list_metrics(Namespace=NS)["Metrics"]
    assert {m["MetricName"] for m in listed} >= {"ErrorRate", "LatencyP95", "Requests", "ActiveFaults"}

    back = cw.telemetry_metrics(NS, "sandbox", minutes=10, region=REGION)
    assert back["series"]["Requests"], "published requests should be readable through GetMetricData"
    assert sum(p["value"] for p in back["series"]["Requests"]) == 400


def test_put_datums_batches_large_payloads(aws, monkeypatch):
    calls = []
    real = cw.client

    def spy(service, region=None, session=None):
        c = real(service, region, session)
        orig = c.put_metric_data
        c.put_metric_data = lambda **kw: (calls.append(len(kw["MetricData"])), orig(**kw))[1]
        return c
    monkeypatch.setattr(cw, "client", spy)
    datums = [d for i in range(130) for d in cw.telemetry_datums(_row(time.time() - i), "sandbox")]
    cw.put_datums(datums, NS, REGION)
    assert sum(calls) == len(datums) and max(calls) <= cw.PUT_BATCH and len(calls) == 3


def test_publisher_sends_only_new_windows(aws):
    rows = []
    pub = cw.TelemetryPublisher(lambda: rows, NS, "sandbox", REGION)
    rows.append(_row(pub.last_t - 10))                 # recorded before the publisher started: skipped
    assert pub.publish_pending() == 0
    rows += [_row(pub.last_t + 1), _row(pub.last_t + 4)]
    assert pub.publish_pending() == 2
    assert pub.publish_pending() == 0                  # nothing new
    rows.append(_row(pub.last_t + 3))
    assert pub.publish_pending() == 1
    st = pub.status()
    assert st["windows_published"] == 3 and st["last_error"] is None and not st["running"]


def test_publisher_records_failure_and_keeps_running(aws, monkeypatch):
    rows = []
    pub = cw.TelemetryPublisher(lambda: rows, NS, "sandbox", REGION, interval_s=0.05)
    rows.append(_row(pub.last_t + 1))
    monkeypatch.setattr(cw, "put_datums", lambda *a, **k: (_ for _ in ()).throw(CloudError("throttled", "Throttling")))
    pub.start()
    time.sleep(0.3)
    assert pub.running and "throttled" in pub.status()["last_error"]
    monkeypatch.undo()
    pub.stop()


def test_ec2_metrics_queries_instance(aws):
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    iid = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    boto3.client("cloudwatch", region_name=REGION).put_metric_data(Namespace="AWS/EC2", MetricData=[
        {"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": iid}], "Value": 42.0,
         "Unit": "Percent"}])
    out = cw.ec2_metrics(iid, minutes=30, period=60, region=REGION)
    assert out["instance_id"] == iid
    assert set(out["series"]) == {"CPUUtilization", "NetworkIn", "NetworkOut", "StatusCheckFailed"}
    assert out["series"]["CPUUtilization"][0]["value"] == 42.0


def test_ec2_metrics_rejects_bad_id(aws):
    with pytest.raises(CloudError) as e:
        cw.ec2_metrics("not-an-instance", region=REGION)
    assert e.value.code == "InvalidInstanceId"


def test_alarms_lists_project_alarms_only(aws):
    c = boto3.client("cloudwatch", region_name=REGION)
    for name in ("CloudResilience-sandbox-HighErrorRate", "SomeoneElse-Alarm"):
        c.put_metric_alarm(AlarmName=name, Namespace=NS, MetricName="ErrorRate", Statistic="Average", Period=60,
                           EvaluationPeriods=2, Threshold=50, ComparisonOperator="GreaterThanThreshold")
    out = cw.alarms(region=REGION)
    assert [a["name"] for a in out] == ["CloudResilience-sandbox-HighErrorRate"]
    assert out[0]["state"] in ("OK", "ALARM", "INSUFFICIENT_DATA") and out[0]["threshold"] == 50
