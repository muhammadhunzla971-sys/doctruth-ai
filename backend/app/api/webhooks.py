import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.github.events import handle_event
from app.github.webhooks import verify_signature
from app.models import WebhookDelivery
from app.workers.trigger import trigger_worker

router = APIRouter()


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_webhook_bytes:
        raise HTTPException(413, "payload too large")
    body = await request.body()
    if len(body) > settings.max_webhook_bytes:
        raise HTTPException(413, "payload too large")
    if not verify_signature(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "invalid signature")

    event = request.headers.get("x-github-event", "")
    delivery_id = request.headers.get("x-github-delivery")
    if not delivery_id:
        raise HTTPException(400, "missing delivery id")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "invalid json") from exc

    if event == "ping":
        return {"status": "pong"}

    try:
        session.add(
            WebhookDelivery(
                delivery_id=delivery_id,
                event=event,
                action=payload.get("action"),
                installation_id=(payload.get("installation") or {}).get("id"),
            )
        )
        await session.flush()
        job = await handle_event(session, settings, event, payload)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {"status": "duplicate"}

    if job:
        await trigger_worker(settings)
        return {"status": "queued", "job_id": str(job.id)}
    return {"status": "ignored"}
