import httpx
from sqlalchemy import select

from app.models import Run
from app.pipeline.publish import PERM_HINT, PREVIEW_MARK, SYNC_MARK, apply_edits, publish
from app.workers.jobs import run_pending
from tests.test_pipeline import NEW, OLD, FakeGitHub, pipeline_for, queue_job

README = "# Guide\n\nThe default timeout is 30s.\nCall `fetch(url, timeout=30)`.\n"
FILES = [{"filename": "a.py", "status": "modified"}, {"filename": "README.md", "status": "modified"}]


def gh_for():
    return FakeGitHub(FILES, {("a.py", "base"): OLD, ("a.py", "head"): NEW, ("README.md", "head"): README})


def edit(before, after, a=1, b=1, path="README.md"):
    return {"kind": "edit", "doc_path": path, "line_start": a, "line_end": b, "before": before, "after": after,
            "method": "deterministic", "confidence": 0.95}


def test_apply_edits_replaces_inside_its_lines_and_skips_stale():
    text = "a 30s\nb 30s\nc\n"
    new, ok, bad = apply_edits(text, [edit("30s", "60s", 2, 2), edit("gone", "x", 3, 3)])
    assert new == "a 30s\nb 60s\nc\n" and len(ok) == 1 and len(bad) == 1


def test_apply_edits_keeps_crlf_line_endings():
    new, ok, _ = apply_edits("a 30s\r\nb\r\n", [edit("30s", "60s")])
    assert new == "a 60s\r\nb\r\n" and ok


async def test_preview_posts_one_comment_and_updates_it_on_rerun(maker):
    gh = gh_for()
    for _ in range(2):
        await queue_job(maker, pr_number=3, head_sha="head")
        await run_pending(maker, 5, pipeline_for(gh))
    comments = gh.w["comments"]
    assert len(comments) == 1 and comments[0]["pr"] == 3 and PREVIEW_MARK in comments[0]["body"]
    assert "timeout=60" in comments[0]["body"] and gh.w["puts"] == [] and gh.w["prs"] == []


async def test_sync_opens_a_doc_pr_on_the_bot_branch_with_fixed_docs(maker):
    gh = gh_for()
    await queue_job(maker, kind="sync", after="head", before="0" * 40)
    await run_pending(maker, 5, pipeline_for(gh))
    assert [p["branch"] for p in gh.w["puts"]] == ["doctruth/sync-7"]
    fixed = gh.w["puts"][0]["text"]
    assert "The default timeout is 60s." in fixed and "fetch(url, timeout=60)" in fixed and "30s" not in fixed
    pr = gh.w["prs"][0]
    assert pr["head"] == "doctruth/sync-7" and pr["base"] == "main" and SYNC_MARK in pr["body"]
    async with maker() as s:
        run = await s.scalar(select(Run))
        assert run.status == "PUBLISHED" and "PR #100" in (await _note(s, run))
    assert any("opened a doc-sync PR" in c["body"] for c in gh.w["comments"] if c["pr"] == 7)


async def _note(s, run):
    from app.models import RunTransition
    t = await s.scalar(select(RunTransition).where(RunTransition.run_id == run.id, RunTransition.to_status == "PUBLISHED"))
    return t.note


async def test_sync_rerun_updates_the_same_pr_instead_of_opening_another(maker):
    gh = gh_for()
    for _ in range(2):
        await queue_job(maker, kind="sync", after="head", before="0" * 40)
        await run_pending(maker, 5, pipeline_for(gh))
        # a fresh job each time, same analysed commit
        async with maker() as s:
            from app.models import Job
            for j in await s.scalars(select(Job)):
                j.dedupe_key = None
            await s.commit()
    assert len(gh.w["prs"]) == 1
    assert len([c for c in gh.w["comments"] if SYNC_MARK in c["body"]]) == 1


async def test_edit_whose_text_changed_since_analysis_becomes_a_question_not_a_guess(maker):
    class Moved(FakeGitHub):
        async def get_file(self, repo, path, ref):  # the doc was edited after analysis
            return "# Guide\n\nTotally rewritten.\n", "blobsha"

    gh = Moved(FILES, gh_for().texts)
    await queue_job(maker, kind="sync", after="head", before="0" * 40)
    await run_pending(maker, 5, pipeline_for(gh))
    assert gh.w["puts"] == [] and gh.w["prs"] == []
    comment = gh.w["comments"][0]["body"]
    assert "changed in the doc since analysis" in comment


async def test_permission_failure_names_the_fix(maker):
    class NoWrite(FakeGitHub):
        async def get_ref(self, repo, branch):
            req = httpx.Request("GET", "https://api.github.com/repos/a/b/git/ref/heads/x")
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))

    gh = NoWrite(FILES, gh_for().texts)
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="sync", commit_sha="head", status="VERIFIED")
        s.add(run)
        await s.flush()
        try:
            await publish(s, run, gh, "sync", [edit("30s", "60s")])
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert PERM_HINT in str(exc)
