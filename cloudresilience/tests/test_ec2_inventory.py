import boto3
import pytest

from cloudresilience.aws import CloudError, ec2_inventory
from .conftest import REGION


def _launch(ec2, n, tags):
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    return ec2.run_instances(ImageId=ami, MinCount=n, MaxCount=n, InstanceType="t3.micro",
                             TagSpecifications=[{"ResourceType": "instance",
                                                 "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}])


def test_inventory_lists_instances_with_summary(aws):
    ec2 = boto3.client("ec2", region_name=REGION)
    _launch(ec2, 2, {"Name": "boutique-node", "Project": "CloudResilience"})
    other = _launch(ec2, 1, {"Name": "unrelated"})
    ec2.stop_instances(InstanceIds=[other["Instances"][0]["InstanceId"]])

    inv = ec2_inventory.inventory(REGION)
    assert inv["summary"]["total"] == 3
    assert inv["summary"]["by_state"] == {"running": 2, "stopped": 1}
    assert inv["summary"]["by_type"] == {"t3.micro": 3}
    row = next(r for r in inv["instances"] if r["name"] == "boutique-node")
    assert row["instance_id"].startswith("i-")
    assert row["tags"]["Project"] == "CloudResilience"
    assert row["availability_zone"].startswith(REGION)


def test_tag_filter_limits_inventory(aws):
    ec2 = boto3.client("ec2", region_name=REGION)
    _launch(ec2, 2, {"Project": "CloudResilience"})
    _launch(ec2, 3, {"Project": "other"})
    rows = ec2_inventory.list_instances(REGION, tag_filter=("Project", "CloudResilience"))
    assert len(rows) == 2


def test_empty_account(aws):
    assert ec2_inventory.inventory(REGION) == {"instances": [], "summary": {"total": 0, "by_state": {}, "by_type": {},
                                                                            "by_az": {}}}


def test_missing_credentials_become_cloud_error(monkeypatch, tmp_path):
    # No moto here: a real (unauthenticated) client must fail with a readable CloudError, not a traceback.
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with pytest.raises(CloudError) as e:
        ec2_inventory.list_instances(REGION)
    assert e.value.code == "NoCredentials"
