import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.llm.scrub import PLACEHOLDER
from app.main import app
from app.models import Job, Run, RunTransition
from app.pipeline.state import RunStatus, transition
from app.workers.jobs import run_pending
from app.workers.queue import claim_next, enqueue, fail
from tests.test_pipeline import NEW, OLD, FakeGitHub, queue_job

DSN = "postgresql+asyncpg://postgres.abc:SuperSecret1@aws-0.pooler.supabase.com:6543/postgres"


async def test_failed_job_error_text_is_scrubbed_before_it_is_stored(maker):
    async with maker() as s:
        job = await enqueue(s, installation_id=1, repo_full_name="a/b", kind="sync", payload={})
        job = await claim_next(s)
        await fail(s, job, f"OSError connecting to {DSN} with token ghp_{'a' * 30}")
        assert "SuperSecret1" not in job.last_error and f"ghp_{'a' * 30}" not in job.last_error
        assert PLACEHOLDER in job.last_error


async def test_run_transition_notes_are_scrubbed(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync", status="RECEIVED")
        s.add(run)
        await s.flush()
        await transition(s, run, RunStatus.FAILED, f"boom {DSN}")
        note = (await s.scalar(select(RunTransition))).note
        assert "SuperSecret1" not in note and "SuperSecret1" not in run.reason


async def test_time_budget_leaves_remaining_jobs_queued(maker):
    for i in range(3):
        async with maker() as s:
            await enqueue(s, installation_id=1, repo_full_name="a/b", kind="preview", payload={"n": i}, dedupe_key=f"k{i}")
            await s.commit()

    async def slow(session, run, job):
        await asyncio.sleep(0.05)

    done = await run_pending(maker, 10, slow, budget_s=0.01)
    assert done == 1  # the first job always runs, then the budget is spent
    async with maker() as s:
        statuses = sorted(j.status for j in await s.scalars(select(Job)))
        assert statuses == ["done", "queued", "queued"]


async def test_oversized_webhook_is_rejected_before_parsing(maker):
    async def session_dep():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_dep
    app.dependency_overrides[get_settings] = lambda: Settings(max_webhook_bytes=100, github_webhook_secret="x")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as c:
        r = await c.post("/webhooks/github", content=b"x" * 500, headers={"x-github-event": "push"})
        assert r.status_code == 413
    app.dependency_overrides.clear()


async def test_dry_run_analyses_but_writes_nothing_to_github(maker):
    from app.pipeline.run import execute_run
    texts = {("a.py", "base"): OLD, ("a.py", "head"): NEW,
             ("README.md", "head"): "The default timeout is 30s.\nCall `fetch(url, timeout=30)`.\n"}
    gh = FakeGitHub([{"filename": "a.py", "status": "modified"}], texts)
    await queue_job(maker, pr_number=3, head_sha="head")

    async def pipeline(session, run, job):
        await execute_run(session, run, job, gh, dry_run=True)

    await run_pending(maker, 5, pipeline)
    async with maker() as s:
        assert (await s.scalar(select(Run))).status == "VERIFIED"
    assert gh.w["comments"] == [] and gh.w["puts"] == [] and gh.w["prs"] == []
