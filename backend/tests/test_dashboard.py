import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.models import EvidenceNode, Run, RunTransition

TOKEN = "s3cret-pass"


@pytest.fixture
async def client(maker):
    app.dependency_overrides[get_settings] = lambda: Settings(dashboard_token=TOKEN)
    app.dependency_overrides[get_sessionmaker] = lambda: maker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as c:
        yield c
    app.dependency_overrides.clear()


async def seed(maker):
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/<b>", kind="sync", pr_number=7, commit_sha="abcdef123", status="PUBLISHED")
        s.add(run)
        await s.flush()
        s.add(RunTransition(run_id=run.id, from_status=None, to_status="RECEIVED", note="<script>x</script>"))
        s.add(EvidenceNode(run_id=run.id, kind="claim", ref="README.md:3", data={
            "verdict": "contradicted", "type": "signature", "reason": "<img src=x onerror=alert(1)>",
            "edit": {"kind": "edit", "before": "f(a)", "after": "f(a, b)", "method": "deterministic", "confidence": 0.95},
            "evidence": [{"ref": "demo.py:3"}]}))
        await s.commit()
        return run.id


async def test_dashboard_is_closed_without_a_token_configured(maker):
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.dependency_overrides[get_sessionmaker] = lambda: maker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as c:
        assert (await c.get("/dashboard")).status_code == 503
    app.dependency_overrides.clear()


async def test_login_wall_wrong_and_right_password(client, maker):
    assert "Sign in" in (await client.get("/dashboard")).text
    assert (await client.get("/api/runs")).status_code == 401
    bad = await client.post("/dashboard/login", content="token=nope", headers={"content-type": "application/x-www-form-urlencoded"})
    assert "Wrong password" in bad.text and "set-cookie" not in bad.headers
    ok = await client.post("/dashboard/login", content=f"token={TOKEN}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert ok.status_code == 303 and "httponly" in ok.headers["set-cookie"].lower()


async def test_runs_page_and_detail_show_edits_and_escape_untrusted_text(client, maker):
    run_id = await seed(maker)
    await client.post("/dashboard/login", content=f"token={TOKEN}", headers={"content-type": "application/x-www-form-urlencoded"})
    home = (await client.get("/dashboard")).text
    assert "a/&lt;b&gt;" in home and "PUBLISHED" in home and "<b>1</b>" in home
    detail = await client.get(f"/dashboard/runs/{run_id}")
    assert "f(a, b)" in detail.text and "demo.py:3" in detail.text
    assert "<script>x</script>" not in detail.text and "<img src=x" not in detail.text
    assert "default-src 'none'" in detail.headers["content-security-policy"]


async def test_api_accepts_bearer_token(client, maker):
    await seed(maker)
    r = await client.get("/api/runs", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["runs"][0]["status"] == "PUBLISHED" and r.json()["counts"] == {"PUBLISHED": 1}
