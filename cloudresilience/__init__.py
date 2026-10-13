"""CloudResilience: the AWS extension of ResilienceAI.

Adds cloud observability and incident reporting around the existing chaos core (chaos/):
  aws/ec2_inventory.py       EC2 infrastructure inventory (read-only)
  aws/cloudwatch_monitor.py  publish chaos telemetry as CloudWatch metrics, read EC2 metrics and alarms
  aws/s3_report_store.py     encrypted, versioned incident reports in S3
  aws/sns_notifier.py        incident notifications through SNS
  api/cloud_routes.py        FastAPI routes mounted on the existing backend under /cloud

Fault injection stays in the local kind sandbox; nothing here changes cloud resources except
writing metrics, report objects and notifications.
"""
