import json
from pathlib import Path

import pytest

REGION = "us-east-1"
FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(autouse=True)
def fake_aws_env(monkeypatch):
    """Every test runs against moto with fake credentials; nothing can reach a real AWS account."""
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "CR_REPORT_BUCKET", "CR_SNS_TOPIC_ARN",
              "CR_PUBLISH_METRICS", "CR_AUTO_REPORT", "CR_EC2_TAG_FILTER", "CR_AWS_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)


@pytest.fixture
def aws():
    from moto import mock_aws
    with mock_aws():
        yield


@pytest.fixture
def squad_state():
    """A real analysed 3-agent run exported from the project's database (trimmed)."""
    return json.loads((FIXTURES / "squad_state.json").read_text())


@pytest.fixture
def bucket(aws):
    import boto3
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="cr-test-reports")
    s3.put_bucket_versioning(Bucket="cr-test-reports", VersioningConfiguration={"Status": "Enabled"})
    return "cr-test-reports"


@pytest.fixture
def topic_with_queue(aws):
    """An SNS topic with an SQS subscriber, so tests can read exactly what was delivered."""
    import boto3
    sns, sqs = boto3.client("sns", region_name=REGION), boto3.client("sqs", region_name=REGION)
    topic = sns.create_topic(Name="cr-test-incidents")["TopicArn"]
    q = sqs.create_queue(QueueName="cr-test-inbox")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn)

    def received():
        msgs = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10).get("Messages", [])
        return [json.loads(m["Body"]) for m in msgs]
    return topic, received
