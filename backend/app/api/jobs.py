import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.github.app_auth import GitHubAppAuth
from app.github.client import GitHubClient
from app.llm.client import LLMClient
from app.models import Job, Run
from app.pipeline.run import execute_run
from app.workers.jobs import run_pending

router = APIRouter()


def make_pipeline(settings: Settings):
    auth = GitHubAppAuth(settings.github_app_id, settings.github_app_private_key)

    async def pipeline(session: AsyncSession, run: Run, job: Job) -> None:
        gh = GitHubClient(auth, job.installation_id or 0)
        llm = LLMClient(settings) if settings.llm_api_key else None  # one client per run = one call budget per run
        try:
            await execute_run(session, run, job, gh, llm, settings.confidence_threshold, settings.dry_run)
        finally:
            await gh.aclose()
            if llm:
                await llm.aclose()

    return pipeline


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
    processed = await run_pending(maker, settings.jobs_per_invocation, make_pipeline(settings), settings.job_time_budget_s)
    return {"processed": processed}
