"""Wires CloudResilience into the existing ResilienceAI backend (chaos/api.py).

chaos/api.py calls attach() once at import time; everything AWS-related lives on this side.
"""
from .api.cloud_routes import make_router
from .service import CloudService


def _squad_state(squad_id=None):
    from chaos import db, squad
    live = squad.public_state()
    if live.get("id") and squad_id in (None, live["id"]):
        return live
    for q in db.squads(100):
        if squad_id in (None, q.get("id")):
            return q
    return None


def attach(app, ingestor, last_rollback, log_event):
    """Mount /cloud routes, start the CloudWatch publisher if CR_PUBLISH_METRICS=1, and return the
    service (its on_rollback method is registered as a rollback hook by the caller)."""
    service = CloudService(telemetry_rows=lambda: list(ingestor.metrics), squad_state=_squad_state,
                           last_rollback=lambda: last_rollback["result"], log=log_event)
    app.include_router(make_router(service))
    if service.cfg.publish_metrics:
        service.start_publishing()
    return service
