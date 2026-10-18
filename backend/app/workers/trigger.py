import logging

import httpx

from app.core.config import Settings

log = logging.getLogger("doctruth.trigger")


async def trigger_worker(settings: Settings) -> None:
    """Fire-and-forget call to our own job runner so work starts right after a webhook.

    The daily Vercel cron is only a safety net. Failure here is harmless: the job stays queued.
    """
    if not settings.public_base_url or not settings.cron_secret:
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{settings.public_base_url.rstrip('/')}/internal/jobs/run",
                headers={"Authorization": f"Bearer {settings.cron_secret}"},
            )
    except httpx.HTTPError:
        log.info("worker trigger did not complete (job stays queued)")
