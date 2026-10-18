import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Job, Run, RunTransition
from app.workers.queue import claim_next, complete, fail

log = logging.getLogger("doctruth.jobs")


async def process_job(session: AsyncSession, job: Job) -> Run:
    """Phase 1: create the Run record. Later phases hand the run to the LangGraph pipeline here."""
    existing = await session.scalar(select(Run).where(Run.job_id == job.id))
    if existing:
        return existing
    p = job.payload
    run = Run(
        job_id=job.id,
        installation_id=job.installation_id or 0,
        repo_full_name=job.repo_full_name,
        kind=job.kind,
        pr_number=p.get("pr_number"),
        commit_sha=p.get("merge_commit_sha") or p.get("head_sha") or p.get("after") or "",
    )
    session.add(run)
    await session.flush()
    session.add(RunTransition(run_id=run.id, from_status=None, to_status="RECEIVED"))
    return run


async def run_pending(maker: async_sessionmaker[AsyncSession], limit: int) -> int:
    processed = 0
    for _ in range(limit):
        async with maker() as session:
            job = await claim_next(session)
            await session.commit()
            if job is None:
                break
            job_id = job.id
        async with maker() as session:
            job = await session.get(Job, job_id)
            try:
                await process_job(session, job)
                await complete(session, job)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - any failure must go back through retry logic
                await session.rollback()
                job = await session.get(Job, job_id)
                await fail(session, job, repr(exc)[:500])
                await session.commit()
                log.exception("job failed", extra={"ctx": {"job_id": str(job_id)}})
        processed += 1
    return processed
