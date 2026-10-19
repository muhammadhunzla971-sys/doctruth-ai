"""Runs the three collectors against GitHub for one job and returns their combined evidence."""
import asyncio
from dataclasses import dataclass

from app.evidence.code_collector import collect_code_changes, language_for
from app.evidence.discussion_collector import (
    Comment, DiscussionEvidence, DiscussionInput, Thread, collect_discussion, linked_issue_numbers,
)
from app.evidence.history_collector import CommitInfo, HistoryEvidence, HistoryInput, collect_history
from app.evidence.symbols import CodeChange, FileChange

ZERO_SHA = "0" * 40
MAX_FILES = 30
MAX_ISSUES = 5
STATUS_MAP = {"changed": "modified", "copied": "added"}


@dataclass
class Scope:
    base: str
    head: str
    pr_number: int | None
    files: list[dict]  # supported source files only
    commits: list[dict]
    all_file_count: int
    truncated: bool


@dataclass
class Evidence:
    changes: list[CodeChange]
    discussion: DiscussionEvidence | None
    history: HistoryEvidence
    files_read: int = 0


async def resolve_scope(gh, repo: str, kind: str, payload: dict) -> Scope:
    pr_number = payload.get("pr_number")
    if kind == "preview":
        pr = await gh.get_pr(repo, pr_number)
        base, head = pr["base"]["sha"], payload.get("head_sha") or pr["head"]["sha"]
    else:
        head = payload.get("merge_commit_sha") or payload.get("after")
        before = payload.get("before") or ""
        base = before if before and before != ZERO_SHA and not pr_number else None
        if base is None:
            base = (await gh.get_commit(repo, head))["parents"][0]["sha"]
        if pr_number is None:
            merged = [p for p in await gh.pulls_for_commit(repo, head) if p.get("merged_at")]
            pr_number = merged[0]["number"] if merged else None
    cmp = await gh.compare(repo, base, head)
    supported = [f for f in cmp["files"] if language_for(f["filename"])]
    return Scope(base, head, pr_number, supported[:MAX_FILES], cmp["commits"], len(cmp["files"]),
                 len(supported) > MAX_FILES)


async def _file_changes(gh, repo: str, scope: Scope) -> list[FileChange]:
    sem = asyncio.Semaphore(6)

    async def fetch(path: str, ref: str) -> str | None:
        async with sem:
            return await gh.get_file_text(repo, path, ref)

    async def one(f: dict) -> FileChange:
        status = STATUS_MAP.get(f["status"], f["status"])
        old_path = f.get("previous_filename") or f["filename"]
        old, new = await asyncio.gather(
            fetch(old_path, scope.base) if status != "added" else asyncio.sleep(0),
            fetch(f["filename"], scope.head) if status != "removed" else asyncio.sleep(0),
        )
        return FileChange(f["filename"], status, f.get("previous_filename"), old or None, new or None)

    return list(await asyncio.gather(*(one(f) for f in scope.files)))


async def _discussion(gh, repo: str, scope: Scope) -> DiscussionEvidence | None:
    if scope.pr_number is None:
        messages = [c["commit"]["message"] for c in scope.commits]
        return collect_discussion(DiscussionInput(commit_messages=messages)) if messages else None
    pr, threads, commits = await asyncio.gather(
        gh.get_pr(repo, scope.pr_number), gh.review_threads(repo, scope.pr_number), gh.pr_commits(repo, scope.pr_number))
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    issues = []
    for n in linked_issue_numbers(text)[:MAX_ISSUES]:
        issue = await gh.get_issue(repo, n)
        if issue:
            issues.append({"number": n, "title": issue.get("title"), "body": issue.get("body")})
    return collect_discussion(DiscussionInput(
        pr_number=scope.pr_number, title=pr.get("title") or "", body=pr.get("body") or "",
        threads=[Thread(t.get("path"), t.get("line"), bool(t["isResolved"]), [
            Comment((c.get("author") or {}).get("login", "unknown"), c.get("body") or "")
            for c in t["comments"]["nodes"]], bool(t.get("isOutdated"))) for t in threads],
        commit_messages=[c["commit"]["message"] for c in commits], issues=issues))


async def _history(gh, repo: str, scope: Scope) -> HistoryEvidence:
    release, tags = await asyncio.gather(gh.latest_release(repo), gh.list_tags(repo))
    return collect_history(HistoryInput(
        commits=[CommitInfo(c["sha"], c["commit"]["message"]) for c in scope.commits],
        renames=[(f["previous_filename"], f["filename"]) for f in scope.files if f.get("previous_filename")],
        latest_release=(release or {}).get("tag_name"),
        tags=[t["name"] for t in tags],
    ))


async def collect_evidence(gh, repo: str, scope: Scope) -> Evidence:
    """The three collectors fetch their inputs concurrently; the parsing itself is pure and synchronous."""
    files, discussion, history = await asyncio.gather(
        _file_changes(gh, repo, scope), _discussion(gh, repo, scope), _history(gh, repo, scope))
    read = sum(1 for f in files if f.new_text or f.old_text)
    return Evidence(collect_code_changes(files), discussion, history, read)
