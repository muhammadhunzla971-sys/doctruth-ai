from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Run, RunTransition


class RunStatus(str, Enum):
    RECEIVED = "RECEIVED"
    SCOPED = "SCOPED"
    EVIDENCE_COLLECTED = "EVIDENCE_COLLECTED"
    ANALYZED = "ANALYZED"
    DRAFTED = "DRAFTED"
    VERIFIED = "VERIFIED"
    PUBLISHED = "PUBLISHED"
    SKIPPED = "SKIPPED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


S = RunStatus
ALLOWED: dict[RunStatus, set[RunStatus]] = {
    S.RECEIVED: {S.SCOPED, S.SKIPPED, S.FAILED},
    S.SCOPED: {S.EVIDENCE_COLLECTED, S.SKIPPED, S.FAILED},
    S.EVIDENCE_COLLECTED: {S.ANALYZED, S.SKIPPED, S.FAILED},
    S.ANALYZED: {S.DRAFTED, S.SKIPPED, S.NEEDS_HUMAN, S.FAILED},
    S.DRAFTED: {S.VERIFIED, S.NEEDS_HUMAN, S.FAILED},
    S.VERIFIED: {S.PUBLISHED, S.NEEDS_HUMAN, S.FAILED},
    S.PUBLISHED: set(),
    S.SKIPPED: set(),
    S.NEEDS_HUMAN: set(),
    S.FAILED: set(),
}


class IllegalTransition(Exception):
    pass


async def transition(session: AsyncSession, run: Run, to: RunStatus, note: str | None = None) -> None:
    current = RunStatus(run.status)
    if to not in ALLOWED[current]:
        raise IllegalTransition(f"{current.value} -> {to.value}")
    session.add(RunTransition(run_id=run.id, from_status=current.value, to_status=to.value, note=note))
    run.status = to.value
    if to in (S.SKIPPED, S.NEEDS_HUMAN, S.FAILED) and note:
        run.reason = note
    await session.flush()
