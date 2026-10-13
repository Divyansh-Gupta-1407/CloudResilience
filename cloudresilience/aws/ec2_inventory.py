"""EC2 infrastructure inventory (read-only: ec2:DescribeInstances only).

    python -m cloudresilience.aws.ec2_inventory [--tag Key=Value] [--json]
"""
import collections

from . import call, client


def _tags(inst):
    return {t["Key"]: t["Value"] for t in inst.get("Tags", [])}


def _row(inst):
    tags = _tags(inst)
    return {
        "instance_id": inst["InstanceId"],
        "name": tags.get("Name"),
        "state": inst["State"]["Name"],
        "type": inst.get("InstanceType"),
        "availability_zone": (inst.get("Placement") or {}).get("AvailabilityZone"),
        "private_ip": inst.get("PrivateIpAddress"),
        "public_ip": inst.get("PublicIpAddress"),
        "launch_time": inst["LaunchTime"].isoformat() if inst.get("LaunchTime") else None,
        "vpc_id": inst.get("VpcId"),
        "platform": inst.get("PlatformDetails") or inst.get("Platform") or "Linux/UNIX",
        "tags": tags,
    }


def list_instances(region=None, tag_filter=None, states=None, session=None):
    """Every instance in the region, newest launch first. tag_filter=("Key", "Value") narrows it."""
    filters = []
    if tag_filter:
        filters.append({"Name": f"tag:{tag_filter[0]}", "Values": [tag_filter[1]]})
    if states:
        filters.append({"Name": "instance-state-name", "Values": list(states)})
    pages = client("ec2", region, session).get_paginator("describe_instances").paginate(Filters=filters)
    rows = [_row(i)
            for page in call("ec2:DescribeInstances", list, pages)
            for res in page["Reservations"] for i in res["Instances"]]
    return sorted(rows, key=lambda r: r["launch_time"] or "", reverse=True)


def summarize(rows):
    """Counts by state, instance type and availability zone, for the dashboard header."""
    return {"total": len(rows),
            "by_state": dict(collections.Counter(r["state"] for r in rows)),
            "by_type": dict(collections.Counter(r["type"] for r in rows)),
            "by_az": dict(collections.Counter(r["availability_zone"] for r in rows))}


def inventory(region=None, tag_filter=None, session=None):
    rows = list_instances(region, tag_filter, session=session)
    return {"instances": rows, "summary": summarize(rows)}


def _cli():
    import argparse
    import json

    from ..config import CloudConfig
    cfg = CloudConfig.from_env()
    ap = argparse.ArgumentParser(prog="python -m cloudresilience.aws.ec2_inventory")
    ap.add_argument("--tag", help="Key=Value tag filter", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    tag = tuple(a.tag.split("=", 1)) if a.tag else cfg.ec2_tag_filter
    inv = inventory(cfg.region, tag)
    if a.json:
        print(json.dumps(inv, indent=2))
        return
    print(f"{inv['summary']['total']} instance(s): {inv['summary']['by_state']}")
    for r in inv["instances"]:
        print(f"  {r['instance_id']:<20} {r['state']:<10} {r['type'] or '?':<12} {r['availability_zone'] or '?':<12} "
              f"{r['name'] or ''}")


if __name__ == "__main__":
    _cli()
