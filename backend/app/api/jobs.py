import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.workers.jobs import run_pending

router = APIRouter()


@router.api_route("/internal/jobs/run", methods=["GET", "POST"])
async def run_jobs(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> dict:
    if not settings.cron_secret:
        raise HTTPException(503, "CRON_SECRET not configured")
    expected = f"Bearer {settings.cron_secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")
    processed = await run_pending(maker, settings.jobs_per_invocation)
    return {"processed": processed}
