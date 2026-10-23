import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Job, Run, RunTransition
from app.pipeline.state import RunStatus, transition
from app.workers.queue import claim_next, complete, fail

log = logging.getLogger("doctruth.jobs")

Pipeline = Callable[[AsyncSession, Run, Job], Awaitable[None]]


async def process_job(session: AsyncSession, job: Job, pipeline: Pipeline | None = None) -> Run:
    """Create the Run record, then hand it to the pipeline (if one is configured)."""
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
    if pipeline is not None:
        await pipeline(session, run, job)
    return run


async def record_dead_job(session: AsyncSession, job: Job) -> None:
    """A job that ran out of retries rolled its run back; keep a FAILED run so the failure stays visible."""
    if await session.scalar(select(Run).where(Run.job_id == job.id)):
        return
    run = Run(job_id=job.id, installation_id=job.installation_id or 0, repo_full_name=job.repo_full_name,
              kind=job.kind, pr_number=job.payload.get("pr_number"))
    session.add(run)
    await session.flush()
    session.add(RunTransition(run_id=run.id, from_status=None, to_status="RECEIVED"))
    await transition(session, run, RunStatus.FAILED, (job.last_error or "job failed")[:500])


async def run_pending(maker: async_sessionmaker[AsyncSession], limit: int, pipeline: Pipeline | None = None,
                      budget_s: float | None = None) -> int:
    """Process up to `limit` jobs; stop starting new ones once `budget_s` is spent (serverless time limit)."""
    processed = 0
    started = time.monotonic()
    for _ in range(limit):
        if budget_s is not None and processed and time.monotonic() - started > budget_s:
            break  # leftover jobs stay queued for the next trigger
        async with maker() as session:
            job = await claim_next(session)
            await session.commit()
            if job is None:
                break
            job_id = job.id
        async with maker() as session:
            job = await session.get(Job, job_id)
            try:
                await process_job(session, job, pipeline)
                await complete(session, job)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - any failure must go back through retry logic
                await session.rollback()
                job = await session.get(Job, job_id)
                await fail(session, job, repr(exc)[:500])
                if job.status == "dead":
                    await record_dead_job(session, job)
                await session.commit()
                log.exception("job failed", extra={"ctx": {"job_id": str(job_id)}})
        processed += 1
    return processed
