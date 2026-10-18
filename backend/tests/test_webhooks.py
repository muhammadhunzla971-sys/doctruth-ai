import hashlib
import hmac
import json

from sqlalchemy import select

from app.models import Installation, Job, Repository
from tests.conftest import SECRET

REPO = {"id": 1, "full_name": "acme/app", "default_branch": "main"}
INST = {"id": 99}


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send(client, event, payload, delivery="d1", secret=SECRET):
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhooks/github",
        content=body,
        headers={"x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": sign(body, secret)},
    )


def pr_payload(action="closed", merged=True, ref="feature", sha="abc", base="main"):
    return {
        "action": action,
        "installation": INST,
        "repository": REPO,
        "pull_request": {
            "number": 7, "merged": merged, "merge_commit_sha": sha,
            "head": {"ref": ref, "sha": "head1"}, "base": {"ref": base},
        },
    }


async def jobs(maker):
    async with maker() as s:
        return list(await s.scalars(select(Job)))


async def test_bad_signature_rejected(client):
    r = await send(client, "pull_request", pr_payload(), secret="wrong")
    assert r.status_code == 401


async def test_ping(client):
    r = await send(client, "ping", {"zen": "hi"})
    assert r.json() == {"status": "pong"}


async def test_merged_pr_queues_sync_job(client, maker):
    r = await send(client, "pull_request", pr_payload())
    assert r.status_code == 202 and r.json()["status"] == "queued"
    js = await jobs(maker)
    assert len(js) == 1 and js[0].kind == "sync" and js[0].repo_full_name == "acme/app"


async def test_duplicate_delivery_is_idempotent(client, maker):
    await send(client, "pull_request", pr_payload(), delivery="same")
    r = await send(client, "pull_request", pr_payload(), delivery="same")
    assert r.json()["status"] == "duplicate"
    assert len(await jobs(maker)) == 1


async def test_push_and_merge_with_same_sha_coalesce(client, maker):
    await send(client, "pull_request", pr_payload(sha="abc"), delivery="d1")
    push = {"installation": INST, "repository": REPO, "ref": "refs/heads/main", "after": "abc", "before": "000"}
    r = await send(client, "push", push, delivery="d2")
    assert r.json()["status"] == "ignored"
    assert len(await jobs(maker)) == 1


async def test_opened_pr_queues_preview(client, maker):
    r = await send(client, "pull_request", pr_payload(action="opened", merged=False))
    assert r.json()["status"] == "queued"
    assert (await jobs(maker))[0].kind == "preview"


async def test_bot_branch_ignored(client, maker):
    r = await send(client, "pull_request", pr_payload(ref="doctruth/sync-7"))
    assert r.json()["status"] == "ignored"
    assert await jobs(maker) == []


async def test_non_default_branch_push_ignored(client, maker):
    push = {"installation": INST, "repository": REPO, "ref": "refs/heads/dev", "after": "abc"}
    r = await send(client, "push", push)
    assert r.json()["status"] == "ignored"


async def test_installation_created_stores_repos(client, maker):
    payload = {
        "action": "created",
        "installation": {"id": 5, "account": {"login": "acme"}},
        "repositories": [{"id": 10, "full_name": "acme/docs"}],
    }
    await send(client, "installation", payload)
    async with maker() as s:
        assert (await s.get(Installation, 5)).account_login == "acme"
        assert (await s.scalar(select(Repository))).full_name == "acme/docs"
