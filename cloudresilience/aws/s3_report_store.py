"""Incident reports in S3: one folder per squad run with report.json (machine-readable) and
report.md (for people), under <prefix>YYYY/MM/DD/<squad_id>/.

Encryption at rest comes from the bucket's default encryption and the bucket policy that denies
non-TLS access (see infra/cloudformation.yaml); versioning keeps every overwrite.

    python -m cloudresilience.aws.s3_report_store list
    python -m cloudresilience.aws.s3_report_store upload path/to/report.json
    python -m cloudresilience.aws.s3_report_store url reports/2026/10/13/<id>/report.md
"""
import json
import re
from datetime import datetime, timezone

from . import CloudError, call, client
from ..reporting import to_markdown

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ReportStore:
    def __init__(self, bucket, prefix="reports/", region=None, session=None):
        if not bucket:
            raise CloudError("no report bucket configured: set CR_REPORT_BUCKET", "NotConfigured")
        self.bucket, self.prefix = bucket, prefix
        self._s3 = client("s3", region, session)

    def folder(self, report):
        sid = str(report["squad_id"])
        if not _SAFE_ID.match(sid):
            raise CloudError(f"unsafe squad id for an S3 key: {sid!r}", "InvalidKey")
        day = datetime.fromisoformat(report["generated_at"]).astimezone(timezone.utc)
        return f"{self.prefix}{day:%Y/%m/%d}/{sid}/"

    def upload(self, report):
        """Write report.json and report.md. Returns their keys and S3 version ids."""
        folder = self.folder(report)
        meta = {"squad-id": str(report["squad_id"]), "environment": str(report.get("environment")),
                "resilience-score": str(report.get("resilience_score")),
                "worst-severity": str(report.get("worst_severity"))}
        out = {"bucket": self.bucket, "folder": folder, "objects": []}
        for name, body, ctype in (("report.json", json.dumps(report, indent=2, default=str), "application/json"),
                                  ("report.md", to_markdown(report), "text/markdown; charset=utf-8")):
            key = folder + name
            r = call("s3:PutObject", self._s3.put_object, Bucket=self.bucket, Key=key, Body=body.encode("utf-8"),
                     ContentType=ctype, Metadata=meta)
            out["objects"].append({"key": key, "version_id": r.get("VersionId"), "etag": r.get("ETag", "").strip('"')})
        return out

    def list(self, limit=50):
        """Reports newest first (one entry per report.json)."""
        pages = self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix)
        items = [o for page in call("s3:ListBucket", list, pages) for o in page.get("Contents", [])
                 if o["Key"].endswith("/report.json")]
        items.sort(key=lambda o: o["LastModified"], reverse=True)
        return [{"key": o["Key"], "markdown_key": o["Key"][:-len("report.json")] + "report.md",
                 "squad_id": o["Key"].rstrip("/").split("/")[-2], "size": o["Size"],
                 "last_modified": o["LastModified"].isoformat()} for o in items[:limit]]

    def _check_key(self, key):
        if not key.startswith(self.prefix) or ".." in key or not key.endswith(("/report.json", "/report.md")):
            raise CloudError(f"not a report key: {key!r}", "InvalidKey")

    def get(self, key):
        self._check_key(key)
        body = call("s3:GetObject", self._s3.get_object, Bucket=self.bucket, Key=key)["Body"].read()
        return json.loads(body) if key.endswith(".json") else body.decode("utf-8")

    def presigned_url(self, key, expires_s=900):
        """Time-limited download link; the bucket itself stays private."""
        self._check_key(key)
        return call("s3:presign", self._s3.generate_presigned_url, "get_object",
                    Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_s)


def _cli():
    import argparse
    from pathlib import Path

    from ..config import CloudConfig
    cfg = CloudConfig.from_env()
    ap = argparse.ArgumentParser(prog="python -m cloudresilience.aws.s3_report_store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("upload", help="upload a report JSON built by cloudresilience.reporting")
    p.add_argument("path")
    p = sub.add_parser("url", help="presigned download link")
    p.add_argument("key")
    a = ap.parse_args()
    store = ReportStore(cfg.report_bucket, cfg.report_prefix, cfg.region)
    if a.cmd == "list":
        for r in store.list():
            print(f"{r['last_modified']}  {r['squad_id']:<20} s3://{cfg.report_bucket}/{r['key']}")
    elif a.cmd == "upload":
        print(json.dumps(store.upload(json.loads(Path(a.path).read_text())), indent=2))
    else:
        print(store.presigned_url(a.key))


if __name__ == "__main__":
    _cli()
