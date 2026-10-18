from tests.conftest import CRON


async def test_requires_auth(client):
    assert (await client.post("/internal/jobs/run")).status_code == 401
    assert (await client.post("/internal/jobs/run", headers={"authorization": "Bearer nope"})).status_code == 401


async def test_runs_with_secret(client):
    r = await client.get("/internal/jobs/run", headers={"authorization": f"Bearer {CRON}"})
    assert r.status_code == 200 and r.json() == {"processed": 0}


async def test_health_and_ready(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    assert (await client.get("/ready")).status_code == 200
