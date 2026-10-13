"""Boto3 clients and the error type every AWS module raises."""
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, NoRegionError

# Adaptive retries absorb throttling; short timeouts keep a dashboard request from hanging on AWS.
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"}, connect_timeout=5, read_timeout=15)


class CloudError(RuntimeError):
    """An AWS call failed. `code` is the AWS error code (e.g. AccessDenied) or a local reason."""

    def __init__(self, message, code="Error"):
        super().__init__(message)
        self.code = code


def client(service, region=None, session=None):
    try:
        return (session or boto3.session.Session()).client(service, region_name=region, config=_BOTO_CONFIG)
    except NoRegionError as e:
        raise CloudError("no AWS region configured: set CR_AWS_REGION or AWS_REGION", "NoRegion") from e


def call(what, fn, *args, **kwargs):
    """Run one boto3 call and turn every AWS failure into a CloudError with a readable message."""
    try:
        return fn(*args, **kwargs)
    except NoCredentialsError as e:
        raise CloudError(f"{what}: no AWS credentials found (set AWS_PROFILE or run 'aws sso login')",
                         "NoCredentials") from e
    except ClientError as e:
        err = e.response.get("Error", {})
        raise CloudError(f"{what}: {err.get('Code')}: {err.get('Message')}", err.get("Code") or "ClientError") from e
    except BotoCoreError as e:
        raise CloudError(f"{what}: {e}", type(e).__name__) from e


def caller_identity(region=None, session=None):
    """Who the backend is acting as. Needs no IAM permission, so it is the cheapest connectivity check."""
    r = call("sts:GetCallerIdentity", client("sts", region, session).get_caller_identity)
    return {"account": r["Account"], "arn": r["Arn"], "user_id": r["UserId"]}
