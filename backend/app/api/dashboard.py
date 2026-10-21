"""Read-only dashboard: runs, their state-machine timeline, proposed edits and evidence. Server-rendered, no JS build."""
import hashlib
import hmac
import uuid
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.models import EvidenceNode, Run, RunTransition

router = APIRouter()
COOKIE = "dt_session"
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
STATUS_CLASS = {"PUBLISHED": "ok", "VERIFIED": "ok", "SKIPPED": "mute", "NEEDS_HUMAN": "warn", "FAILED": "bad"}

CSS = """
:root{--bg:#fafaf8;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--line:#e4e4de;--ok:#1a7f4b;--warn:#a15c00;--bad:#b3261e;--acc:#2754c5}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececE8;--mut:#9a9a93;--card:#1d1d1b;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#f0746b;--acc:#7fa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px 48px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
a{color:var(--acc);text-decoration:none}.sub{color:var(--mut);font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px}
.stat b{display:block;font-size:20px}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}th{color:var(--mut);font-weight:600;font-size:12px}
.pill{font-size:12px;font-weight:600;padding:1px 8px;border-radius:99px;border:1px solid currentColor}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mute{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:8px 0;overflow-x:auto}
pre{margin:6px 0;font:13px/1.45 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}.del{color:var(--bad)}.add{color:var(--ok)}
form{max-width:340px;margin:20vh auto 0}input,button{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid var(--line);width:100%;margin-top:8px}button{background:var(--acc);color:#fff;border:0;cursor:pointer}
@media(max-width:640px){th:nth-child(4),td:nth-child(4){display:none}}
"""


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"doctruth-dashboard", hashlib.sha256).hexdigest()


def require_auth(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if not settings.dashboard_token:
        raise HTTPException(503, "DASHBOARD_TOKEN not configured")
    sent = request.cookies.get(COOKIE, "")
    bearer = request.headers.get("authorization", "")
    ok = hmac.compare_digest(sent, _session_value(settings.dashboard_token)) or \
        hmac.compare_digest(bearer, f"Bearer {settings.dashboard_token}")
    if not ok:
        raise HTTPException(401, "login required")


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{CSS}</style><main>{body}</main></html>")
    return HTMLResponse(html, status_code=status, headers={"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"})


def pill(status: str) -> str:
    return f"<span class='pill {STATUS_CLASS.get(status, '')}'>{escape(status)}</span>"


def login_page(error: str = "") -> HTMLResponse:
    msg = f"<p class=bad>{escape(error)}</p>" if error else ""
    return page("DocTruth login", f"<form method=post action=/dashboard/login><h1>DocTruth AI</h1>"
                f"<p class=sub>Dashboard password</p>{msg}<input type=password name=token autofocus autocomplete=current-password>"
                f"<button>Sign in</button></form>")


@router.post("/dashboard/login")
async def login(request: Request, settings: Settings = Depends(get_settings)):
    if not settings.dashboard_token:
        raise HTTPException(503, "DASHBOARD_TOKEN not configured")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    token = (form.get("token") or [""])[0]
    if not hmac.compare_digest(token, settings.dashboard_token):
        return login_page("Wrong password")
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(COOKIE, _session_value(token), httponly=True, secure=request.url.scheme == "https",
                    samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


@router.get("/dashboard/logout")
async def logout():
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


async def _runs(session: AsyncSession, limit: int = 100):
    return list(await session.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit)))


async def _counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Run.status, func.count()).group_by(Run.status))
    return {s: n for s, n in rows}


def run_json(r: Run) -> dict:
    return {"id": str(r.id), "repo": r.repo_full_name, "kind": r.kind, "pr_number": r.pr_number, "commit_sha": r.commit_sha,
            "status": r.status, "reason": r.reason, "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("/dashboard", response_class=HTMLResponse)
async def index(request: Request, settings: Settings = Depends(get_settings),
                maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker)):
    try:
        require_auth(request, settings)
    except HTTPException as exc:
        if exc.status_code == 401:
            return login_page()
        raise
    async with maker() as s:
        runs, counts = await _runs(s), await _counts(s)
    total = sum(counts.values())
    stats = "".join(f"<div class=stat><b>{n}</b><span class=sub>{escape(k)}</span></div>"
                    for k, n in [("runs", total), ("published", counts.get("PUBLISHED", 0)),
                                 ("skipped", counts.get("SKIPPED", 0)), ("needs human", counts.get("NEEDS_HUMAN", 0)),
                                 ("failed", counts.get("FAILED", 0))])
    rows = "".join(
        f"<tr><td><a href='/dashboard/runs/{r.id}'>{escape(r.repo_full_name)}</a><br><span class=sub>{escape(r.kind)}"
        f"{' · PR #' + str(r.pr_number) if r.pr_number else ''}</span></td><td>{pill(r.status)}</td>"
        f"<td class=sub>{escape((r.reason or '')[:140])}</td><td class=sub>{r.created_at:%Y-%m-%d %H:%M}</td></tr>" for r in runs)
    table = (f"<table><tr><th>Repository</th><th>Status</th><th>Reason</th><th>When (UTC)</th></tr>{rows}</table>"
             if runs else "<p class=sub>No runs yet. Push a change to an installed repository.</p>")
    return page("DocTruth AI", f"<h1>DocTruth AI</h1><p class=sub>Runs, newest first · <a href=/dashboard/logout>sign out</a></p>"
                f"<div class=stats>{stats}</div>{table}")


def _claim_html(n: EvidenceNode) -> str:
    d, e = n.data or {}, (n.data or {}).get("edit") or {}
    out = f"<div class=card><b>{escape(n.ref)}</b> {pill(d.get('verdict', '?').upper())} <span class=sub>{escape(str(d.get('type', '')))}</span>"
    out += f"<p>{escape(d.get('reason', ''))}</p>"
    if e.get("kind") == "edit":
        out += (f"<pre class=del>- {escape(e.get('before', ''))}</pre><pre class=add>+ {escape(e.get('after', ''))}</pre>"
                f"<span class=sub>{escape(str(e.get('method', '')))} · confidence {escape(str(e.get('confidence', '')))}</span>")
    elif e.get("kind") == "question":
        out += f"<p class=warn><b>Question:</b> {escape(e.get('question', ''))}</p>"
    ev = ", ".join(escape(x.get("ref", "")) for x in d.get("evidence", []))
    if ev:
        out += f"<br><span class=sub>Evidence: {ev}</span>"
    for note in d.get("notes", []):
        out += f"<br><span class='sub warn'>{escape(str(note))}</span>"
    return out + "</div>"


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(run_id: uuid.UUID, request: Request, settings: Settings = Depends(get_settings),
                     maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker)):
    try:
        require_auth(request, settings)
    except HTTPException as exc:
        if exc.status_code == 401:
            return login_page()
        raise
    async with maker() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        steps = list(await s.scalars(select(RunTransition).where(RunTransition.run_id == run.id).order_by(RunTransition.at)))
        claims = [n for n in await s.scalars(select(EvidenceNode).where(EvidenceNode.run_id == run.id)) if n.kind == "claim"]
        kinds = dict((await s.execute(select(EvidenceNode.kind, func.count()).where(EvidenceNode.run_id == run.id)
                                      .group_by(EvidenceNode.kind))).all())
    ref = f"PR #{run.pr_number}" if run.pr_number else f"commit {run.commit_sha[:7]}"
    timeline = "".join(f"<tr><td>{pill(t.to_status)}</td><td class=sub>{escape(t.note or '')}</td><td class=sub>{t.at:%H:%M:%S}</td></tr>"
                       for t in steps)
    graph = " · ".join(f"{n} {escape(k)}" for k, n in sorted(kinds.items())) or "no evidence stored"
    body = (f"<p><a href=/dashboard>&larr; all runs</a></p><h1>{escape(run.repo_full_name)}</h1>"
            f"<p class=sub>{escape(run.kind)} · {escape(ref)} · {pill(run.status)}</p>"
            f"<h2>Timeline</h2><table>{timeline}</table><h2>Evidence graph</h2><p class=sub>{graph}</p>"
            f"<h2>Findings ({len(claims)})</h2>" + ("".join(_claim_html(c) for c in claims) or "<p class=sub>No documentation findings.</p>"))
    return page(f"Run {run.repo_full_name}", body)


@router.get("/api/runs")
async def api_runs(_: None = Depends(require_auth), maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker)):
    async with maker() as s:
        return JSONResponse({"runs": [run_json(r) for r in await _runs(s)], "counts": await _counts(s)})
