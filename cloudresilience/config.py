"""Settings for the AWS integration, read from environment variables.

Credentials are never configured here: boto3's default chain is used (AWS_PROFILE, environment
variables, SSO, or an instance/task role), so no key ever lives in the repository.

  CR_AWS_REGION           region for every client (falls back to AWS_REGION / AWS_DEFAULT_REGION)
  CR_REPORT_BUCKET        S3 bucket for incident reports              (reports disabled when unset)
  CR_REPORT_PREFIX        key prefix inside the bucket                (default "reports/")
  CR_SNS_TOPIC_ARN        SNS topic for incident notifications        (notifications disabled when unset)
  CR_METRIC_NAMESPACE     CloudWatch namespace for chaos telemetry    (default "CloudResilience/Boutique")
  CR_ENVIRONMENT          dimension value that separates environments (default "sandbox")
  CR_PUBLISH_METRICS      "1" to stream telemetry windows to CloudWatch when the backend starts
  CR_AUTO_REPORT          "1" to upload a report (and notify) automatically after every squad rollback
  CR_EC2_TAG_FILTER       optional "Key=Value" tag that limits the EC2 inventory
"""
import os
from dataclasses import dataclass
from typing import Optional


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class CloudConfig:
    region: Optional[str]
    report_bucket: Optional[str]
    report_prefix: str
    sns_topic_arn: Optional[str]
    metric_namespace: str
    environment: str
    publish_metrics: bool
    auto_report: bool
    ec2_tag_filter: Optional[tuple]

    @classmethod
    def from_env(cls):
        tag = os.environ.get("CR_EC2_TAG_FILTER", "").strip()
        tag_filter = tuple(tag.split("=", 1)) if "=" in tag else None
        prefix = os.environ.get("CR_REPORT_PREFIX", "reports/").strip()
        return cls(
            region=os.environ.get("CR_AWS_REGION") or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION"),
            report_bucket=os.environ.get("CR_REPORT_BUCKET") or None,
            report_prefix=prefix if not prefix or prefix.endswith("/") else prefix + "/",
            sns_topic_arn=os.environ.get("CR_SNS_TOPIC_ARN") or None,
            metric_namespace=os.environ.get("CR_METRIC_NAMESPACE", "CloudResilience/Boutique"),
            environment=os.environ.get("CR_ENVIRONMENT", "sandbox"),
            publish_metrics=_flag("CR_PUBLISH_METRICS"),
            auto_report=_flag("CR_AUTO_REPORT"),
            ec2_tag_filter=tag_filter,
        )

    def public(self):
        """Settings that are safe to show in the dashboard (no secrets are ever held here)."""
        return {"region": self.region, "report_bucket": self.report_bucket, "report_prefix": self.report_prefix,
                "sns_topic_arn": self.sns_topic_arn, "metric_namespace": self.metric_namespace,
                "environment": self.environment, "publish_metrics": self.publish_metrics,
                "auto_report": self.auto_report,
                "ec2_tag_filter": "=".join(self.ec2_tag_filter) if self.ec2_tag_filter else None}
