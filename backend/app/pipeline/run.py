"""Pipeline driver. Phase 2 covers RECEIVED -> SCOPED -> EVIDENCE_COLLECTED (or SKIPPED)."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence.collect import collect_evidence, resolve_scope
from app.evidence.graph import build_graph, persist_graph
from app.models import Job, Run
from app.pipeline.state import RunStatus, transition


async def execute_run(session: AsyncSession, run: Run, job: Job, gh) -> None:
    repo = job.repo_full_name
    scope = await resolve_scope(gh, repo, job.kind, job.payload)
    run.commit_sha = scope.head
    run.pr_number = scope.pr_number
    if not scope.files:
        await transition(session, run, RunStatus.SKIPPED, "internal_only: no JS/TS/Python source files changed")
        return
    note = f"{len(scope.files)} source files" + (" (truncated)" if scope.truncated else "")
    await transition(session, run, RunStatus.SCOPED, note)

    evidence = await collect_evidence(gh, repo, scope)
    if not evidence.changes:
        await transition(session, run, RunStatus.SKIPPED, "internal_only: no public symbol changes")
        return
    graph = build_graph(evidence.changes, evidence.discussion, evidence.history,
                        [f["filename"] for f in scope.files], scope.head)
    await persist_graph(session, run.id, graph)
    await transition(session, run, RunStatus.EVIDENCE_COLLECTED,
                     f"{len(evidence.changes)} code changes, {len(graph.nodes)} nodes, {len(graph.edges)} edges")
