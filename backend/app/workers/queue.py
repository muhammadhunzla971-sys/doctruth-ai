"""Postgres-backed job queue (no Redis needed on free serverless hosting)."""
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.models.tables import utcnow

STALE_AFTER = timedelta(minutes=5)  # a "running" job older than this is assumed crashed


async def enqueue(
    session: AsyncSession,
    *,
    installation_id: int | None,
    repo_full_name: str,
    kind: str,
    payload: dict,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        kind=kind,
        payload=payload,
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_next(session: AsyncSession) -> Job | None:
    while True:
        now = utcnow()
        stmt = (
            select(Job)
            .where(
                or_(
                    and_(Job.status == "queued", Job.run_after <= now),
                    and_(Job.status == "running", Job.updated_at < now - STALE_AFTER),
                )
            )
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None
        if job.attempts >= job.max_attempts:
            job.status = "dead"
            job.last_error = job.last_error or "exceeded max attempts"
            await session.flush()
            continue
        job.status = "running"
        job.attempts += 1
        job.updated_at = now
        await session.flush()
        return job


async def complete(session: AsyncSession, job: Job) -> None:
    job.status = "done"
    job.last_error = None
    await session.flush()


async def fail(session: AsyncSession, job: Job, error: str) -> None:
    job.last_error = error
    if job.attempts >= job.max_attempts:
        job.status = "dead"
    else:
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=10 * 2**job.attempts)
    await session.flush()
