import pytest

from app.models import Run
from app.pipeline.state import ALLOWED, IllegalTransition, RunStatus, transition


async def new_run(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync")
        s.add(run)
        await s.commit()
        return run.id


async def test_happy_path_and_log(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync", status="RECEIVED")
        s.add(run)
        await s.flush()
        for st in ("SCOPED", "EVIDENCE_COLLECTED", "ANALYZED", "DRAFTED", "VERIFIED", "PUBLISHED"):
            await transition(s, run, RunStatus(st))
        assert run.status == "PUBLISHED"


async def test_illegal_transition_rejected(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync", status="RECEIVED")
        s.add(run)
        await s.flush()
        with pytest.raises(IllegalTransition):
            await transition(s, run, RunStatus.PUBLISHED)


async def test_terminal_states_are_final(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync", status="RECEIVED")
        s.add(run)
        await s.flush()
        await transition(s, run, RunStatus.SKIPPED, "internal only")
        assert run.reason == "internal only"
        with pytest.raises(IllegalTransition):
            await transition(s, run, RunStatus.SCOPED)


def test_every_status_has_rules():
    assert set(ALLOWED) == set(RunStatus)
