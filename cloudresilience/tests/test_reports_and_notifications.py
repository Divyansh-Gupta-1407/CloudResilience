import json
from urllib.parse import urlparse

import boto3
import pytest

from cloudresilience import reporting
from cloudresilience.aws import CloudError
from cloudresilience.aws.s3_report_store import ReportStore
from cloudresilience.aws.sns_notifier import SUBJECT_MAX, Notifier, subject
from .conftest import REGION

ROLLBACK = {"at": "2026-10-11T13:40:00+00:00", "recovery_verified": True, "recovery_time_s": 15.2,
            "actions": ["scaled recommendationservice to 1"], "by": "operator"}


# ------------------------------------------------------------------ report content

def test_report_matches_backend_formulas(squad_state):
    r = reporting.build_report(squad_state, ROLLBACK, "sandbox", generated_at="2026-10-11T13:41:00+00:00")
    # 100 - (0.6 x 37 + 0.4 x mean(37, 20, 0)) = 70.2 -> 70, same as chaos/api.py /findings
    assert r["resilience_score"] == 70 and r["resilience_label"] == "Resilient"
    assert r["worst_severity"] == "Medium"
    assert [f["target"] for f in r["findings"]] == ["checkoutservice", "recommendationservice", "adservice"]
    assert r["findings"][0]["suspected_cause"] == "network latency on every call into the service on checkoutservice (+1000 ms)"
    assert r["findings"][0]["fix"]["model"] == "gemma3:4b"
    assert len(r["faults_injected"]) == 3 and all(f["applied"] for f in r["faults_injected"])
    json.dumps(r)   # fully serialisable


def test_markdown_and_summary(squad_state):
    r = reporting.build_report(squad_state, ROLLBACK, "sandbox")
    md = reporting.to_markdown(r)
    assert md.startswith("# CloudResilience incident report: squad 20261011-133241")
    assert "| 1 | Medium | checkoutservice | Network latency |" in md
    assert "recovery verified in 15.2 s" in md
    text = reporting.summary_text(r, "https://example.invalid/report.md")
    assert "Resilience score: 70/100 (Resilient)" in text and "Full report: https://example.invalid/report.md" in text


def test_unanalysed_run_still_reports(squad_state):
    squad_state.update(severity=None, fixes={}, status="STOPPED")
    r = reporting.build_report(squad_state)
    assert r["resilience_score"] is None and r["findings"] == [] and r["worst_severity"] is None
    assert "not analysed" in reporting.to_markdown(r)


# ------------------------------------------------------------------ S3

def test_upload_list_get_and_presign(bucket, squad_state):
    store = ReportStore(bucket, "reports/", REGION)
    report = reporting.build_report(squad_state, ROLLBACK, generated_at="2026-10-11T13:41:00+00:00")
    up = store.upload(report)
    assert up["folder"] == "reports/2026/10/11/20261011-133241/"
    assert all(o["version_id"] for o in up["objects"]), "bucket versioning keeps every upload"

    head = boto3.client("s3", region_name=REGION).head_object(Bucket=bucket, Key=up["folder"] + "report.md")
    assert head["ContentType"].startswith("text/markdown")
    assert head["Metadata"]["resilience-score"] == "70"

    listed = store.list()
    assert [x["squad_id"] for x in listed] == ["20261011-133241"]
    assert store.get(listed[0]["key"])["resilience_score"] == 70
    assert store.get(listed[0]["markdown_key"]).startswith("# CloudResilience incident report")

    url = urlparse(store.presigned_url(listed[0]["markdown_key"], 60))
    assert url.path.endswith("/report.md") and "Signature" in url.query or "X-Amz-Signature" in url.query


def test_rejects_keys_outside_reports(bucket):
    store = ReportStore(bucket, "reports/", REGION)
    for key in ("secrets/creds.json", "reports/../secrets/report.json", "reports/2026/x/notes.txt"):
        with pytest.raises(CloudError):
            store.presigned_url(key)


def test_rejects_unsafe_squad_id(bucket, squad_state):
    squad_state["id"] = "../../escape"
    with pytest.raises(CloudError) as e:
        ReportStore(bucket, "reports/", REGION).upload(reporting.build_report(squad_state))
    assert e.value.code == "InvalidKey"


def test_missing_bucket_config():
    with pytest.raises(CloudError) as e:
        ReportStore(None)
    assert e.value.code == "NotConfigured"


# ------------------------------------------------------------------ SNS

def test_notification_delivered_with_attributes(topic_with_queue, squad_state):
    topic, received = topic_with_queue
    report = reporting.build_report(squad_state, ROLLBACK, "sandbox")
    out = Notifier(topic, REGION).notify_incident(report, "https://example.invalid/r.md")
    assert out["message_id"]
    [msg] = received()
    assert msg["Subject"] == "[CloudResilience] Medium: squad 20261011-133241, score 70/100"
    assert "#1 Medium: Network latency on checkoutservice" in msg["Message"]
    assert msg["MessageAttributes"]["worst_severity"]["Value"] == "Medium"
    assert msg["MessageAttributes"]["resilience_score"]["Value"] == "70"


def test_subject_respects_sns_limit(squad_state):
    squad_state["id"] = "x" * 120
    assert len(subject(reporting.build_report(squad_state))) == SUBJECT_MAX
