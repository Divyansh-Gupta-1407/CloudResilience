"""Incident notifications through SNS. Subscribers (email, SMS, Lambda, chat webhooks) are managed
on the topic itself; this module only publishes (sns:Publish on one topic ARN)."""
from . import CloudError, call, client
from ..reporting import summary_text

SUBJECT_MAX = 100   # SNS limit for email subjects


def subject(report):
    score = report.get("resilience_score")
    text = (f"[CloudResilience] {report.get('worst_severity') or 'Run ended'}: squad {report['squad_id']}, "
            f"score {score if score is not None else 'n/a'}/100")
    return text[:SUBJECT_MAX]


class Notifier:
    def __init__(self, topic_arn, region=None, session=None):
        if not topic_arn:
            raise CloudError("no SNS topic configured: set CR_SNS_TOPIC_ARN", "NotConfigured")
        self.topic_arn = topic_arn
        self._sns = client("sns", region, session)

    def notify_incident(self, report, report_url=None):
        """Publish the report summary. Message attributes let subscribers filter (e.g. only Critical)."""
        attrs = {"environment": {"DataType": "String", "StringValue": str(report.get("environment"))},
                 "worst_severity": {"DataType": "String", "StringValue": str(report.get("worst_severity") or "None")},
                 "squad_id": {"DataType": "String", "StringValue": str(report["squad_id"])}}
        if report.get("resilience_score") is not None:
            attrs["resilience_score"] = {"DataType": "Number", "StringValue": str(report["resilience_score"])}
        r = call("sns:Publish", self._sns.publish, TopicArn=self.topic_arn, Subject=subject(report),
                 Message=summary_text(report, report_url), MessageAttributes=attrs)
        return {"message_id": r["MessageId"], "topic_arn": self.topic_arn}
