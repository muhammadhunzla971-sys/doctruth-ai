from sqlalchemy import select

from app.models import EvidenceEdge, EvidenceNode, Job, Run, RunTransition
from app.workers.jobs import run_pending
from app.workers.queue import enqueue

OLD = "def fetch(url, timeout=30):\n    return url\n"
NEW = "def fetch(url, timeout=60):\n    return url\n"


class FakeGitHub:
    """Same methods as GitHubClient, answering from in-memory fixtures."""

    def __init__(self, files, texts, pr_body="", threads=None, commits=None):
        self.files, self.texts, self.pr_body = files, texts, pr_body
        self.threads = threads or []
        self.commits = commits or [{"sha": "c" * 40, "commit": {"message": "Raise timeout"}}]

    async def get_pr(self, repo, n):
        return {"title": "Raise timeout", "body": self.pr_body, "base": {"sha": "base"}, "head": {"sha": "head"}}

    async def get_commit(self, repo, sha):
        return {"parents": [{"sha": "base"}]}

    async def compare(self, repo, base, head):
        return {"files": self.files, "commits": self.commits}

    async def get_file_text(self, repo, path, ref):
        return self.texts.get((path, ref))

    async def pr_commits(self, repo, n):
        return self.commits

    async def review_threads(self, repo, n):
        return self.threads

    async def get_issue(self, repo, n):
        return {"title": "Flaky", "body": ""}

    async def latest_release(self, repo):
        return {"tag_name": "v1.0.0"}

    async def list_tags(self, repo):
        return [{"name": "v1.0.0"}]

    async def get_tree(self, repo, sha):
        return sorted({p for p, _ in self.texts} | {"README.md"})

    async def pulls_for_commit(self, repo, sha):
        return [{"number": 7, "merged_at": "2026-01-01T00:00:00Z"}]


def pipeline_for(gh):
    from app.pipeline.run import execute_run

    async def pipeline(session, run, job):
        await execute_run(session, run, job, gh)

    return pipeline


async def queue_job(maker, kind="preview", **payload):
    async with maker() as s:
        await enqueue(s, installation_id=1, repo_full_name="a/b", kind=kind, payload=payload)
        await s.commit()


async def test_preview_run_collects_evidence_and_stores_graph(maker):
    gh = FakeGitHub(
        [{"filename": "a.py", "status": "modified"}, {"filename": "README.md", "status": "modified"}],
        {("a.py", "base"): OLD, ("a.py", "head"): NEW,
         ("README.md", "head"): "# Guide\n\nThe default timeout is 30s.\nCall `fetch(url, timeout=30)`.\n"},
        pr_body="Raises the timeout. The default timeout is 30s. Closes #4",
        threads=[{"isResolved": True, "isOutdated": False, "path": "a.py", "line": 1,
                  "comments": {"nodes": [{"body": "ok, doubling the timeout", "author": {"login": "ann"}}]}}],
    )
    await queue_job(maker, pr_number=3, head_sha="head", base_ref="main")
    assert await run_pending(maker, 5, pipeline_for(gh)) == 1
    async with maker() as s:
        run = await s.scalar(select(Run))
        assert run.status == "ANALYZED" and run.commit_sha == "head" and run.pr_number == 3
        steps = [t.to_status for t in await s.scalars(select(RunTransition).order_by(RunTransition.at))]
        assert steps == ["RECEIVED", "SCOPED", "EVIDENCE_COLLECTED", "ANALYZED"]
        claims = [n for n in await s.scalars(select(EvidenceNode)) if n.kind == "claim"]
        assert {c.data["type"] for c in claims} == {"default_value", "signature"}
        assert all(c.data["verdict"] == "contradicted" for c in claims)
        nodes = list(await s.scalars(select(EvidenceNode)))
        kinds = {n.kind for n in nodes}
        assert {"pr", "file", "symbol", "comment_thread"} <= kinds
        pr = next(n for n in nodes if n.kind == "pr")
        assert pr.data["linked_issues"] == [{"number": 4, "title": "Flaky"}]
        assert "contradicts" in {e.kind for e in await s.scalars(select(EvidenceEdge))}
        assert (await s.scalar(select(Job))).status == "done"


async def test_sync_run_without_pr_number_finds_the_merged_pr(maker):
    gh = FakeGitHub([{"filename": "a.py", "status": "modified"}], {("a.py", "base"): OLD, ("a.py", "head"): NEW})
    await queue_job(maker, kind="sync", after="head", before="0" * 40)
    await run_pending(maker, 5, pipeline_for(gh))
    async with maker() as s:
        run = await s.scalar(select(Run))
        assert run.pr_number == 7 and run.status == "SKIPPED" and "no documentation affected" in run.reason


async def test_docs_only_change_is_skipped_as_internal_only(maker):
    gh = FakeGitHub([{"filename": "README.md", "status": "modified"}], {})
    await queue_job(maker, pr_number=3, head_sha="head")
    await run_pending(maker, 5, pipeline_for(gh))
    async with maker() as s:
        run = await s.scalar(select(Run))
        assert run.status == "SKIPPED" and "internal_only" in run.reason
        assert list(await s.scalars(select(EvidenceNode))) == []


async def test_source_change_without_public_symbol_change_is_skipped(maker):
    gh = FakeGitHub([{"filename": "a.py", "status": "modified"}],
                    {("a.py", "base"): "x = 1\n", ("a.py", "head"): "x = 2\n"})
    await queue_job(maker, pr_number=3, head_sha="head")
    await run_pending(maker, 5, pipeline_for(gh))
    async with maker() as s:
        run = await s.scalar(select(Run))
        assert run.status == "SKIPPED" and "no public symbol changes" in run.reason


async def test_failing_pipeline_retries_then_leaves_a_failed_run(maker):
    class Broken(FakeGitHub):
        async def compare(self, *a):
            raise RuntimeError("github is down")

    async with maker() as s:
        await enqueue(s, installation_id=1, repo_full_name="a/b", kind="preview",
                      payload={"pr_number": 3, "head_sha": "head"}, max_attempts=1)
        await s.commit()
    await run_pending(maker, 5, pipeline_for(Broken([], {})))
    async with maker() as s:
        job = await s.scalar(select(Job))
        run = await s.scalar(select(Run))
        assert job.status == "dead" and "github is down" in job.last_error
        assert run.status == "FAILED" and "github is down" in run.reason
