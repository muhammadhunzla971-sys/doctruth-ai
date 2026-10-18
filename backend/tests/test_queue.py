from datetime import timedelta

from sqlalchemy import select

from app.models import Job, Run, RunTransition
from app.models.tables import utcnow
from app.workers.jobs import run_pending
from app.workers.queue import claim_next, complete, enqueue, fail


async def make_job(session, **kw):
    job = await enqueue(session, installation_id=1, repo_full_name="a/b", kind="sync", payload={"after": "x"}, **kw)
    await session.commit()
    return job


async def test_claim_and_complete(maker):
    async with maker() as s:
        job = await make_job(s)
        claimed = await claim_next(s)
        assert claimed.id == job.id and claimed.status == "running" and claimed.attempts == 1
        await complete(s, claimed)
        assert claimed.status == "done"
        assert await claim_next(s) is None


async def test_fail_retries_with_backoff_then_dead(maker):
    async with maker() as s:
        await make_job(s, max_attempts=2)
        job = await claim_next(s)
        await fail(s, job, "boom")
        assert job.status == "queued" and job.run_after > utcnow() + timedelta(seconds=5)
        assert await claim_next(s) is None  # still backing off
        job.run_after = utcnow() - timedelta(seconds=1)
        job = await claim_next(s)
        await fail(s, job, "boom again")
        assert job.status == "dead"


async def test_stale_running_job_is_reclaimed(maker):
    async with maker() as s:
        job = await make_job(s)
        job.status = "running"
        job.updated_at = utcnow() - timedelta(minutes=10)
        await s.commit()
        assert (await claim_next(s)).id == job.id


async def test_run_pending_creates_run_once(maker):
    async with maker() as s:
        await make_job(s)
    assert await run_pending(maker, 5) == 1
    async with maker() as s:
        runs = list(await s.scalars(select(Run)))
        assert len(runs) == 1 and runs[0].status == "RECEIVED" and runs[0].commit_sha == "x"
        assert len(list(await s.scalars(select(RunTransition)))) == 1
        assert (await s.scalar(select(Job))).status == "done"
