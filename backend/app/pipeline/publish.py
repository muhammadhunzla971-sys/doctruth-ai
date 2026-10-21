"""Phase 5: turn verified edits into a doc-sync PR (merged code) or a PR comment (open PR). Idempotent: safe to re-run."""
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repository, Run
from app.pipeline.state import RunStatus, transition

BOT_BRANCH = "doctruth/sync-"
MAX_BODY = 60000
PREVIEW_MARK = "<!-- doctruth:preview -->"
SYNC_MARK = "<!-- doctruth:sync -->"
PERM_HINT = ("GitHub App cannot write: give it Contents + Pull requests = Read and write, "
             "then accept the new permissions on the installation")


def apply_edits(text: str, edits: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """Replace each edit's `before` inside its own lines. Returns (new text, applied, skipped). Bottom-up keeps line numbers valid."""
    crlf = "\r\n" in text
    lines = text.replace("\r\n", "\n").split("\n")
    applied, skipped = [], []
    for e in sorted(edits, key=lambda e: e["line_start"], reverse=True):
        a, b = e["line_start"] - 1, e["line_end"]
        seg = "\n".join(lines[a:b])
        if e["before"] and e["before"] in seg:
            lines[a:b] = seg.replace(e["before"], e["after"], 1).split("\n")
            applied.append(e)
        else:
            skipped.append(e)  # the doc changed since analysis: never guess
    out = "\n".join(lines)
    return (out.replace("\n", "\r\n") if crlf else out), applied[::-1], skipped[::-1]


def _edit_md(e: dict) -> str:
    ev = ", ".join(f"`{x['ref']}`" for x in e.get("evidence", [])[:3])
    before = e["before"].replace("\n", "\n- ")
    after = e["after"].replace("\n", "\n+ ")
    return (f"**`{e['doc_path']}:{e['line_start']}`** ({e['method']}, confidence {e['confidence']})\n"
            f"{e.get('reason', '')}\n```diff\n- {before}\n+ {after}\n```\n" + (f"Evidence: {ev}\n" if ev else ""))


def render(items: list[dict], run: Run, *, skipped: list[dict] = (), preview: bool = False) -> str:
    edits = [i for i in items if i["kind"] == "edit"]
    qs = [i for i in items if i["kind"] == "question"] + list(skipped)
    ref = f"PR #{run.pr_number}" if run.pr_number else f"commit `{run.commit_sha[:7]}`"
    if preview:
        head = f"{PREVIEW_MARK}\n### DocTruth: docs affected by this PR\nBased on {ref} at `{run.commit_sha[:7]}`. "
        head += "After merge I will open a doc-sync PR with these edits.\n"
    else:
        head = f"{SYNC_MARK}\n### DocTruth: documentation sync\nCode change: {ref}. Review and merge if correct.\n"
    parts = [head]
    if edits:
        parts.append(f"#### Proposed edits ({len(edits)})\n" + "\n".join(_edit_md(e) for e in edits))
    if qs:
        parts.append(f"#### Questions ({len(qs)})\nI am not confident enough to edit these, so I am asking.\n" +
                     "\n".join(f"- {q.get('question') or ('`' + q['doc_path'] + ':' + str(q['line_start']) + '` changed in the doc since analysis, not edited')}"
                               for q in qs))
    parts.append(f"<sub>run `{run.id}`</sub>")
    return "\n\n".join(parts)[:MAX_BODY]


async def upsert_comment(gh, repo: str, number: int, marker: str, body: str) -> None:
    for c in await gh.list_comments(repo, number):
        if marker in (c.get("body") or ""):
            await gh.update_comment(repo, c["id"], body)
            return
    await gh.create_comment(repo, number, body)


async def publish(session: AsyncSession, run: Run, gh, kind: str, items: list[dict]) -> None:
    repo = run.repo_full_name
    try:
        await _publish(session, run, gh, kind, items)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 404):
            raise RuntimeError(f"{PERM_HINT} ({exc.response.status_code} {exc.request.method} {exc.request.url.path})") from exc
        raise


async def _publish(session: AsyncSession, run: Run, gh, kind: str, items: list[dict]) -> None:
    repo = run.repo_full_name
    edits = [i for i in items if i["kind"] == "edit"]
    questions = [i for i in items if i["kind"] == "question"]

    if kind == "preview":
        await upsert_comment(gh, repo, run.pr_number, PREVIEW_MARK, render(items, run, preview=True))
        await transition(session, run, RunStatus.PUBLISHED,
                         f"comment on PR #{run.pr_number}: {len(edits)} edits, {len(questions)} questions")
        return

    base = await session.scalar(select(Repository.default_branch).where(Repository.full_name == repo)) or "main"
    branch = f"{BOT_BRANCH}{run.pr_number or run.commit_sha[:7]}"
    applied, skipped = [], []
    by_doc: dict[str, list[dict]] = {}
    for e in edits:
        by_doc.setdefault(e["doc_path"], []).append(e)
    if by_doc:
        if await gh.get_ref(repo, branch) is None:
            await gh.create_ref(repo, branch, run.commit_sha)
        else:
            await gh.reset_ref(repo, branch, run.commit_sha)  # rebuild from the analysed commit: re-runs stay idempotent
        for path, es in by_doc.items():
            got = await gh.get_file(repo, path, branch)
            if got is None:
                skipped += es
                continue
            text, sha = got
            new, ok, bad = apply_edits(text, es)
            skipped += bad
            if ok:
                await gh.put_file(repo, path, new, f"docs: sync {path} with code ({len(ok)} edits)", branch, sha)
                applied += ok

    if applied:
        body = render(applied + questions, run, skipped=skipped)
        title = f"docs: sync documentation with {'PR #' + str(run.pr_number) if run.pr_number else run.commit_sha[:7]}"
        pr = await gh.find_open_pr(repo, branch)
        pr = await (gh.update_pr(repo, pr["number"], title, body) if pr else gh.create_pr(repo, title, body, branch, base))
        if run.pr_number:
            await upsert_comment(gh, repo, run.pr_number, SYNC_MARK,
                                 f"{SYNC_MARK}\nDocTruth opened a doc-sync PR: #{pr['number']}")
        await transition(session, run, RunStatus.PUBLISHED,
                         f"PR #{pr['number']}: {len(applied)} edits, {len(questions) + len(skipped)} questions")
    elif run.pr_number:
        await upsert_comment(gh, repo, run.pr_number, SYNC_MARK, render(items, run, skipped=skipped))
        await transition(session, run, RunStatus.PUBLISHED, f"comment on PR #{run.pr_number}: questions only")
    else:
        q = "; ".join(i.get("question", "") for i in questions)[:400]
        await transition(session, run, RunStatus.NEEDS_HUMAN, f"no PR to ask on, no edit applied: {q}")
