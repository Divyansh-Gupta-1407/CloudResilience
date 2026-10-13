"""FastAPI routes for the AWS integration, mounted on the existing backend under /cloud.

| Method | Path                          | Purpose                                                  |
|--------|-------------------------------|----------------------------------------------------------|
| GET    | /cloud/status                 | AWS identity, configuration, publisher state, activity   |
| GET    | /cloud/inventory              | EC2 instances with counts by state / type / AZ           |
| GET    | /cloud/ec2/{id}/metrics       | CPU, network, status checks from CloudWatch              |
| GET    | /cloud/telemetry              | chaos telemetry read back from CloudWatch                |
| GET    | /cloud/alarms                 | CloudResilience alarm states                             |
| POST   | /cloud/telemetry/publish/start| stream telemetry windows to CloudWatch                   |
| POST   | /cloud/telemetry/publish/stop | stop streaming                                           |
| POST   | /cloud/reports                | upload an incident report to S3 (+ SNS notification)     |
| GET    | /cloud/reports                | reports stored in S3, newest first                       |
| GET    | /cloud/reports/url?key=...    | presigned download link                                  |
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..aws import CloudError

_HTTP = {"NotConfigured": 409, "NotFound": 404, "InvalidKey": 400, "InvalidInstanceId": 400,
         "NoCredentials": 503, "NoRegion": 503, "AccessDenied": 403, "AccessDeniedException": 403,
         "UnauthorizedOperation": 403, "NoSuchKey": 404, "NoSuchBucket": 404}


class ReportRequest(BaseModel):
    squad_id: Optional[str] = Field(None, description="squad run to report; default: the latest one")
    notify: bool = Field(True, description="also publish to the SNS topic when one is configured")


async def _aws(fn, *args, **kwargs):
    """Run a blocking boto3 operation off the event loop and map AWS failures to HTTP errors."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except CloudError as e:
        raise HTTPException(_HTTP.get(e.code, 502), str(e)) from e


def make_router(service):
    r = APIRouter(prefix="/cloud", tags=["cloud"])

    @r.get("/status")
    async def status():
        return await run_in_threadpool(service.status)

    @r.get("/inventory")
    async def inventory():
        return await _aws(service.inventory)

    @r.get("/ec2/{instance_id}/metrics")
    async def ec2_metrics(instance_id: str, minutes: int = Query(60, ge=5, le=1440)):
        return await _aws(service.ec2_metrics, instance_id, minutes)

    @r.get("/telemetry")
    async def telemetry(minutes: int = Query(30, ge=5, le=1440)):
        return await _aws(service.telemetry_metrics, minutes)

    @r.get("/alarms")
    async def alarms():
        return await _aws(service.alarms)

    @r.post("/telemetry/publish/start")
    def publish_start():
        return service.start_publishing()

    @r.post("/telemetry/publish/stop")
    def publish_stop():
        return service.stop_publishing()

    @r.post("/reports")
    async def upload_report(req: ReportRequest = ReportRequest()):
        return await _aws(service.publish_report, req.squad_id, req.notify)

    @r.get("/reports")
    async def reports(limit: int = Query(50, ge=1, le=500)):
        return await _aws(service.list_reports, limit)

    @r.get("/reports/url")
    async def report_url(key: str):
        return await _aws(service.report_url, key)

    return r
