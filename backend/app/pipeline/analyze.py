"""Phase 3 stage: extract doc claims and check them deterministically against this change's evidence."""
import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.claims.drift import Finding, detect_drift
from app.claims.extract import extract_claims
from app.evidence.collect import Evidence, Scope
from app.models import EvidenceEdge, EvidenceNode

MAX_DOCS = 20
MAX_FINDINGS = 100
MANIFESTS = ("pyproject.toml", "package.json")


def is_doc(path: str) -> bool:
    return path.endswith(".md") and (path.count("/") == 0 or path.startswith("docs/"))


async def analyze_docs(gh, repo: str, scope: Scope, evidence: Evidence) -> list[Finding]:
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
    found = detect_drift(claims, evidence.changes, gone, set(tree), {m: texts[m] for m in manifests if texts.get(m)})
    return found[:MAX_FINDINGS]


async def persist_findings(session: AsyncSession, run_id: uuid.UUID, ids: dict, findings: list[Finding]) -> None:
    for f in findings:
        c = f.claim
        node_id = uuid.uuid4()
        session.add(EvidenceNode(id=node_id, run_id=run_id, kind="claim", ref=c.ref[:500], data={
            "type": c.type, "text": c.text[:300], "heading": c.heading, "line_start": c.line_start,
            "line_end": c.line_end, "verdict": f.verdict, "reason": f.reason, "confidence": f.confidence,
            "evidence": f.evidence}))
        kind = "contradicts" if f.verdict in ("contradicted", "stale_example") else "documents"
        targets = [("symbol", f"{s.file}:{s.name}") for s in f.symbols]
        if f.file_path:
            key = ("file", f.file_path)
            if key not in ids:
                ids[key] = uuid.uuid4()
                session.add(EvidenceNode(id=ids[key], run_id=run_id, kind="file", ref=f.file_path, data={"removed": True}))
            targets.append(key)
        await session.flush()  # the claim and any new file node must exist before their edges
        for key in dict.fromkeys(targets):
            if key in ids:
                session.add(EvidenceEdge(run_id=run_id, src_id=node_id, dst_id=ids[key], kind=kind))
    await session.flush()
