"""The least-privilege policy: no wildcard actions, writes scoped, and the standalone JSON policy
grants exactly what the CloudFormation-managed policy grants."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "cloudresilience-backend-policy.json"
TEMPLATE = ROOT / "infra" / "cloudformation.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


# Treat CloudFormation short-form tags (!Ref, !Sub, ...) as plain values.
_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node)))


def _actions(statements):
    out = {}
    for s in statements:
        a = s["Action"]
        out[s["Sid"]] = sorted([a] if isinstance(a, str) else a)
    return out


def test_policy_is_least_privilege():
    doc = json.loads(POLICY.read_text())
    actions = [a for s in doc["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
    assert all("*" not in a for a in actions), "no wildcard actions"
    assert all(s["Effect"] == "Allow" for s in doc["Statement"])
    writes = {"cloudwatch:PutMetricData", "s3:PutObject", "sns:Publish"}
    for s in doc["Statement"]:
        acts = {s["Action"]} if isinstance(s["Action"], str) else set(s["Action"])
        if acts & writes:
            assert s["Resource"] != "*" or "Condition" in s, f"{s['Sid']} writes without a resource or condition scope"
    assert not any(a.startswith(("iam:", "ec2:Run", "ec2:Terminate", "ec2:Stop", "s3:Delete")) for a in actions)


def test_policy_matches_cloudformation():
    tpl = yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)
    cfn = tpl["Resources"]["BackendPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert _actions(cfn) == _actions(json.loads(POLICY.read_text())["Statement"])


@pytest.mark.skipif(shutil.which("cfn-lint") is None and not (Path(sys.executable).parent / "cfn-lint.exe").exists(),
                    reason="cfn-lint not installed")
def test_cloudformation_template_lints():
    exe = shutil.which("cfn-lint") or str(Path(sys.executable).parent / "cfn-lint.exe")
    r = subprocess.run([exe, str(TEMPLATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
