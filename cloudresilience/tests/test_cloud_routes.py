"""The /cloud API end to end: FastAPI -> CloudService -> boto3 -> moto."""
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloudresilience.api.cloud_routes import make_router
from cloudresilience.config import CloudConfig
from cloudresilience.service import CloudService


def _app(squad_state=None, rows=(), rollback=None, **cfg):
    config = replace(CloudConfig.from_env(), **cfg)
    logs = []
    service = CloudService(telemetry_rows=lambda: list(rows),
                           squad_state=lambda squad_id=None: squad_state if squad_id in (None, (squad_state or {}).get("id")) else None,
                           last_rollback=lambda: rollback, log=lambda m, c="INFO": logs.append((c, m)), config=config)
    app = FastAPI()
    app.include_router(make_router(service))
    return TestClient(app), service, logs


def test_status_reports_identity_and_features(aws):
    client, _, _ = _app()
    s = client.get("/cloud/status").json()
    assert s["connected"] and s["identity"]["account"]
    assert s["features"] == {"inventory": True, "metrics": True, "reports": False, "notifications": False}
    assert s["publisher"]["running"] is False


def test_inventory_and_alarms_endpoints(aws):
    client, _, _ = _app()
    assert client.get("/cloud/inventory").json()["summary"]["total"] == 0
    assert client.get("/cloud/alarms").json() == []
    assert client.get("/cloud/ec2/nope/metrics").status_code == 400


def test_report_requires_bucket(aws, squad_state):
    client, _, _ = _app(squad_state)
    r = client.post("/cloud/reports", json={})
    assert r.status_code == 409 and "CR_REPORT_BUCKET" in r.json()["detail"]


def test_report_unknown_squad_is_404(bucket, squad_state):
    client, _, _ = _app(squad_state, report_bucket=bucket)
    assert client.post("/cloud/reports", json={"squad_id": "19990101-000000"}).status_code == 404


def test_upload_notify_list_and_download(bucket, topic_with_queue, squad_state):
    topic, received = topic_with_queue
    rollback = {"at": "2026-10-11T13:40:00+00:00", "recovery_verified": True, "recovery_time_s": 12.0, "actions": []}
    client, service, logs = _app(squad_state, rollback=rollback, report_bucket=bucket, sns_topic_arn=topic)

    r = client.post("/cloud/reports", json={"notify": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resilience_score"] == 70 and body["notification"]["message_id"]
    assert "report.md" in body["report_url"]
    [msg] = received()
    assert "recovery verified in 12.0 s" in msg["Message"], "rollback after the run started belongs to the report"

    listed = client.get("/cloud/reports").json()
    assert len(listed) == 1
    url = client.get("/cloud/reports/url", params={"key": listed[0]["markdown_key"]}).json()["url"]
    assert url.startswith("https://")
    assert client.get("/cloud/reports/url", params={"key": "other/secret.json"}).status_code == 400
    assert service.activity[0]["notified"] and any(c == "CLOUD" for c, _ in logs)


def test_old_rollback_is_not_attached(bucket, squad_state):
    stale = {"at": "2020-01-01T00:00:00+00:00", "recovery_verified": False, "actions": []}
    client, service, _ = _app(squad_state, rollback=stale, report_bucket=bucket)
    client.post("/cloud/reports", json={})
    key = client.get("/cloud/reports").json()[0]["key"]
    assert service._store().get(key)["rollback"] is None


def test_publisher_start_stop(aws):
    now = time.time()
    rows = [{"t": now + 1, "n": 10, "error_rate": 0.0, "p50_ms": 20, "p95_ms": 30, "faults": []}]
    client, service, _ = _app(rows=rows)
    assert client.post("/cloud/telemetry/publish/start").json()["running"] is True
    assert service.publisher.publish_pending() == 1
    client.post("/cloud/telemetry/publish/stop")
    service.publisher._thread.join(timeout=20)
    assert client.get("/cloud/status").json()["publisher"]["running"] is False


@pytest.mark.parametrize("auto", [True, False])
def test_rollback_hook_auto_report(bucket, squad_state, auto):
    _, service, _ = _app(squad_state, report_bucket=bucket, auto_report=auto)
    rollback = {"at": "2026-10-11T13:40:00+00:00", "recovery_verified": True, "recovery_time_s": 9.0, "actions": []}
    t = service.on_rollback(squad_state, rollback)
    if not auto:
        assert t is None and service.list_reports() == []
        return
    t.join(timeout=30)
    [rep] = service.list_reports()
    assert service._store().get(rep["key"])["rollback"]["recovery_time_s"] == 9.0


def test_rollback_hook_never_raises(aws, squad_state):
    # Bucket configured but it does not exist: the failure is recorded, not raised into the rollback.
    _, service, logs = _app(squad_state, report_bucket="does-not-exist-cr", auto_report=True)
    service.on_rollback(squad_state, None).join(timeout=30)
    assert "error" in service.activity[0] and any(c == "ERROR" for c, _ in logs)
