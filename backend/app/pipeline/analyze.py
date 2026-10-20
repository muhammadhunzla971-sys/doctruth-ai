"""Phase 3 stage: extract doc claims and check them deterministically against this change's evidence."""
import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.claims.drift import DriftContext, Finding, detect_drift
from app.claims.extract import extract_claims
from app.evidence.collect import Evidence, Scope
from app.models import EvidenceEdge, EvidenceNode

MAX_DOCS = 20
MAX_FINDINGS = 100
MANIFESTS = ("pyproject.toml", "package.json")


def is_doc(path: str) -> bool:
    return path.endswith(".md") and (path.count("/") == 0 or path.startswith("docs/"))


async def analyze_docs(gh, repo: str, scope: Scope, evidence: Evidence) -> tuple[list[Finding], DriftContext]:
    tree = await gh.get_tree(repo, scope.head)
    docs = sorted(p for p in tree if is_doc(p))[:MAX_DOCS]
    changed = {f["filename"] for f in scope.all_files if f["status"] != "removed"}
    manifests = [m for m in MANIFESTS if m in changed]
    sem = asyncio.Semaphore(6)

    async def read(path: str) -> tuple[str, str | None]:
        async with sem:
            return path, await gh.get_file_text(repo, path, scope.head)

    texts = dict(await asyncio.gather(*(read(p) for p in docs + manifests)))
    claims = [c for d in docs if texts.get(d) for c in extract_claims(d, texts[d])]
    gone = {f["previous_filename"]: f["filename"] for f in scope.all_files if f.get("previous_filename")}
    gone.update({f["filename"]: None for f in scope.all_files if f["status"] == "removed"})
    ctx = DriftContext(evidence.changes, gone, set(tree), {m: texts[m] for m in manifests if texts.get(m)})
    return detect_drift(claims, ctx.changes, ctx.gone, ctx.tree, ctx.manifests)[:MAX_FINDINGS], ctx


async def register_files(session: AsyncSession, run_id: uuid.UUID, ids: dict, findings: list[Finding]) -> None:
    """File nodes that only the doc analysis knows about (removed/renamed paths, manifests), created before edits cite them."""
    for f in findings:
        for path, data in ((f.file_path, {"removed": True}), (f.meta.get("evidence_file"), {})):
            if path and ("file", path) not in ids:
                ids[("file", path)] = uuid.uuid4()
                session.add(EvidenceNode(id=ids[("file", path)], run_id=run_id, kind="file", ref=path, data=data))
    await session.flush()


async def persist_findings(session: AsyncSession, run_id: uuid.UUID, ids: dict, findings: list[Finding]) -> None:
    """Claim nodes (with verdict, evidence and proposed edit), then their edges. Nodes are flushed first (foreign keys)."""
    nodes = []
    for f in findings:
        c = f.claim
        node_id = uuid.uuid4()
        nodes.append((node_id, f))
        session.add(EvidenceNode(id=node_id, run_id=run_id, kind="claim", ref=c.ref[:500], data={
            "type": c.type, "text": c.text[:600], "heading": c.heading, "line_start": c.line_start,
            "line_end": c.line_end, "verdict": f.verdict, "reason": f.reason, "confidence": f.confidence,
            "evidence": f.evidence, "sources_conflict": f.sources_conflict, "notes": f.notes, "edit": f.edit}))
    await session.flush()
    for node_id, f in nodes:
        kind = "contradicts" if f.verdict in ("contradicted", "stale_example") else "documents"
        targets = [("symbol", f"{s.file}:{s.name}") for s in f.symbols]
        targets += [("file", p) for p in (f.file_path, f.meta.get("evidence_file")) if p]
        for key in dict.fromkeys(targets):
            if key in ids:
                session.add(EvidenceEdge(run_id=run_id, src_id=node_id, dst_id=ids[key], kind=kind))
    await session.flush()
