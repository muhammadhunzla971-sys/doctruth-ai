import uuid

from fastapi import FastAPI, Request

from app.api import health, jobs, webhooks
from app.core.logging import request_id_var, setup_logging

setup_logging()

app = FastAPI(title="DocTruth AI", version="0.1.0")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response


app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(jobs.router)
