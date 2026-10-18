"""Turns a verified GitHub webhook payload into DB updates and (at most) one queued job."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Installation, Job, Repository
from app.workers.queue import enqueue

BOT_BRANCH_PREFIX = "doctruth/"
ZERO_SHA = "0" * 40


async def _ensure_installation(session: AsyncSession, installation_id: int, login: str | None = None) -> Installation:
    inst = await session.get(Installation, installation_id)
    if inst is None:
        inst = Installation(id=installation_id, account_login=login)
        session.add(inst)
        await session.flush()
    return inst


async def _upsert_repo(session: AsyncSession, installation_id: int, repo: dict) -> Repository:
    row = await session.scalar(select(Repository).where(Repository.github_repo_id == repo["id"]))
    if row is None:
        row = Repository(
            installation_id=installation_id,
            github_repo_id=repo["id"],
            full_name=repo["full_name"],
            default_branch=repo.get("default_branch") or "main",
        )
        session.add(row)
    else:
        row.installation_id = installation_id
        row.full_name = repo["full_name"]
        row.active = True
        if repo.get("default_branch"):
            row.default_branch = repo["default_branch"]
    await session.flush()
    return row


async def _deactivate_repos(session: AsyncSession, repos: list[dict]) -> None:
    for repo in repos:
        row = await session.scalar(select(Repository).where(Repository.github_repo_id == repo["id"]))
        if row:
            row.active = False


async def _handle_installation(session: AsyncSession, payload: dict) -> None:
    inst_data = payload["installation"]
    action = payload.get("action")
    inst = await _ensure_installation(session, inst_data["id"], (inst_data.get("account") or {}).get("login"))
    if action == "created":
        for repo in payload.get("repositories", []):
            await _upsert_repo(session, inst.id, repo)
    elif action in ("deleted", "suspend"):
        inst.suspended = True
        if action == "deleted":
            rows = await session.scalars(select(Repository).where(Repository.installation_id == inst.id))
            for row in rows:
                row.active = False
    elif action == "unsuspend":
        inst.suspended = False


async def _handle_installation_repositories(session: AsyncSession, payload: dict) -> None:
    inst = await _ensure_installation(session, payload["installation"]["id"])
    for repo in payload.get("repositories_added", []):
        await _upsert_repo(session, inst.id, repo)
    await _deactivate_repos(session, payload.get("repositories_removed", []))


async def _enqueue_unique(
    session: AsyncSession, settings: Settings, *, installation_id: int, repo: str, kind: str, key: str, payload: dict
) -> Job | None:
    exists = await session.scalar(select(Job.id).where(Job.dedupe_key == key))
    if exists:
        return None
    return await enqueue(
        session,
        installation_id=installation_id,
        repo_full_name=repo,
        kind=kind,
        payload=payload,
        dedupe_key=key,
        max_attempts=settings.job_max_attempts,
    )


async def _handle_pull_request(session: AsyncSession, settings: Settings, payload: dict) -> Job | None:
    inst_id = payload["installation"]["id"]
    await _ensure_installation(session, inst_id)
    repo = await _upsert_repo(session, inst_id, payload["repository"])
    pr = payload["pull_request"]
    if pr["head"]["ref"].startswith(BOT_BRANCH_PREFIX):
        return None  # never react to our own doc-sync PRs
    action = payload.get("action")
    base = {"pr_number": pr["number"], "head_sha": pr["head"]["sha"], "base_ref": pr["base"]["ref"]}

    if action == "closed" and pr.get("merged") and pr["base"]["ref"] == repo.default_branch:
        sha = pr.get("merge_commit_sha") or pr["head"]["sha"]
        return await _enqueue_unique(
            session, settings, installation_id=inst_id, repo=repo.full_name, kind="sync",
            key=f"sync:{repo.full_name}:{sha}", payload={**base, "merge_commit_sha": sha},
        )
    if action in ("opened", "synchronize", "reopened"):
        return await _enqueue_unique(
            session, settings, installation_id=inst_id, repo=repo.full_name, kind="preview",
            key=f"preview:{repo.full_name}:{pr['number']}:{pr['head']['sha']}", payload=base,
        )
    return None


async def _handle_push(session: AsyncSession, settings: Settings, payload: dict) -> Job | None:
    inst_id = payload["installation"]["id"]
    await _ensure_installation(session, inst_id)
    repo = await _upsert_repo(session, inst_id, payload["repository"])
    after = payload.get("after", "")
    if payload.get("deleted") or after == ZERO_SHA or payload.get("ref") != f"refs/heads/{repo.default_branch}":
        return None
    # A merged PR also fires a push with the same SHA: the dedupe key makes them one job.
    return await _enqueue_unique(
        session, settings, installation_id=inst_id, repo=repo.full_name, kind="sync",
        key=f"sync:{repo.full_name}:{after}", payload={"after": after, "before": payload.get("before", "")},
    )


async def handle_event(session: AsyncSession, settings: Settings, event: str, payload: dict) -> Job | None:
    if "installation" not in payload:
        return None
    if event == "installation":
        await _handle_installation(session, payload)
    elif event == "installation_repositories":
        await _handle_installation_repositories(session, payload)
    elif event == "pull_request":
        return await _handle_pull_request(session, settings, payload)
    elif event == "push":
        return await _handle_push(session, settings, payload)
    return None
