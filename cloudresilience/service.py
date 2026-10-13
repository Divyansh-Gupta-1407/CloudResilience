"""The AWS side of the backend in one object: configuration, the CloudWatch publisher, report upload
and notification. The FastAPI routes (api/cloud_routes.py) are a thin layer over this class, and the
chaos backend reaches it only through the callables passed in (see integration.py), so it can be
tested without Kubernetes, Ollama or the chaos package.
"""
import collections
import threading
from datetime import datetime, timezone

from . import reporting
from .aws import CloudError, caller_identity, cloudwatch_monitor, ec2_inventory
from .aws.s3_report_store import ReportStore
from .aws.sns_notifier import Notifier
from .config import CloudConfig


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CloudService:
    def __init__(self, telemetry_rows=lambda: [], squad_state=lambda squad_id=None: None,
                 last_rollback=lambda: None, log=lambda msg, cat="INFO": None, config=None, session=None):
        self.cfg = config or CloudConfig.from_env()
        self._session = session
        self._squad_state, self._last_rollback, self._log = squad_state, last_rollback, log
        self.publisher = cloudwatch_monitor.TelemetryPublisher(
            telemetry_rows, self.cfg.metric_namespace, self.cfg.environment, self.cfg.region, session=session)
        self.activity = collections.deque(maxlen=50)      # uploads and notifications, newest first
        self._report_lock = threading.Lock()

    # ------------------------------------------------------------------ status

    def status(self):
        try:
            identity, identity_error = caller_identity(self.cfg.region, self._session), None
        except CloudError as e:
            identity, identity_error = None, str(e)
        return {"connected": identity is not None, "identity": identity, "identity_error": identity_error,
                "config": self.cfg.public(),
                "features": {"inventory": True, "metrics": True, "reports": bool(self.cfg.report_bucket),
                             "notifications": bool(self.cfg.sns_topic_arn)},
                "publisher": self.publisher.status(), "activity": list(self.activity)[:10]}

    # ------------------------------------------------------------------ read-only monitoring

    def inventory(self):
        return ec2_inventory.inventory(self.cfg.region, self.cfg.ec2_tag_filter, self._session)

    def ec2_metrics(self, instance_id, minutes=60):
        return cloudwatch_monitor.ec2_metrics(instance_id, minutes, region=self.cfg.region, session=self._session)

    def telemetry_metrics(self, minutes=30):
        return cloudwatch_monitor.telemetry_metrics(self.cfg.metric_namespace, self.cfg.environment, minutes,
                                                    region=self.cfg.region, session=self._session)

    def alarms(self):
        return cloudwatch_monitor.alarms(region=self.cfg.region, session=self._session)

    # ------------------------------------------------------------------ telemetry publishing

    def start_publishing(self):
        self.publisher.start()
        self._log(f"CloudWatch publisher started: telemetry -> {self.cfg.metric_namespace} "
                  f"(Environment={self.cfg.environment}).", "CLOUD")
        return self.publisher.status()

    def stop_publishing(self):
        self.publisher.stop()
        self._log("CloudWatch publisher stopped.", "CLOUD")
        return self.publisher.status()

    # ------------------------------------------------------------------ reports + notifications

    def _store(self):
        return ReportStore(self.cfg.report_bucket, self.cfg.report_prefix, self.cfg.region, self._session)

    def list_reports(self, limit=50):
        return self._store().list(limit)

    def report_url(self, key, expires_s=900):
        return {"key": key, "url": self._store().presigned_url(key, expires_s), "expires_s": expires_s}

    def publish_report(self, squad_id=None, notify=True, state=None, rollback=None):
        """Build the incident report for one squad run (default: the latest), upload it to S3 and, when
        a topic is configured, notify SNS subscribers with a presigned link to the Markdown report."""
        state = state or self._squad_state(squad_id)
        if not state or not state.get("id"):
            raise CloudError(f"no squad run {'with id ' + squad_id if squad_id else 'recorded yet'}", "NotFound")
        if rollback is None:
            rollback = self._rollback_for(state)
        report = reporting.build_report(state, rollback, self.cfg.environment)
        with self._report_lock:
            uploaded = self._store().upload(report)
            md_key = next(o["key"] for o in uploaded["objects"] if o["key"].endswith(".md"))
            url = self._store().presigned_url(md_key, 7 * 24 * 3600)   # SigV4 maximum: 7 days
            result = {"squad_id": report["squad_id"], "resilience_score": report["resilience_score"],
                      "worst_severity": report["worst_severity"], "upload": uploaded, "report_url": url,
                      "notification": None}
            if notify and self.cfg.sns_topic_arn:
                result["notification"] = Notifier(self.cfg.sns_topic_arn, self.cfg.region,
                                                  self._session).notify_incident(report, url)
        self.activity.appendleft({"at": _now(), "kind": "report", "squad_id": report["squad_id"],
                                  "folder": uploaded["folder"], "notified": bool(result["notification"])})
        self._log(f"Incident report for squad {report['squad_id']} uploaded to s3://{self.cfg.report_bucket}/"
                  f"{uploaded['folder']}" + (" and sent to SNS." if result["notification"] else "."), "CLOUD")
        return result

    def _rollback_for(self, state):
        """The last rollback, but only if it happened after this run started (it belongs to this run)."""
        rb = self._last_rollback()
        if not rb or not state.get("started"):
            return None
        try:
            return rb if datetime.fromisoformat(rb["at"]).timestamp() >= state["started"] else None
        except (KeyError, ValueError, TypeError):
            return None

    def on_rollback(self, state, rollback):
        """Rollback hook: with CR_AUTO_REPORT=1, report the run that was just rolled back. Runs in the
        background so the rollback response is never delayed by AWS."""
        if not self.cfg.auto_report or not self.cfg.report_bucket or not state or not state.get("id"):
            return None

        def work():
            try:
                self.publish_report(state=state, rollback=rollback)
            except Exception as e:  # noqa: BLE001 - an AWS failure must never affect the chaos backend
                self.activity.appendleft({"at": _now(), "kind": "report", "squad_id": state.get("id"), "error": str(e)})
                self._log(f"Automatic incident report failed: {e}", "ERROR")

        t = threading.Thread(target=work, name="cloud-auto-report", daemon=True)
        t.start()
        return t
