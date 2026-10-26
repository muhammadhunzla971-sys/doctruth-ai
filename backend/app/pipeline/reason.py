"""fusion rules, semantic judgement, rewrite and critic. Deterministic first, the LLM only where a rule cannot decide."""
import difflib
import logging
import re
from dataclasses import dataclass

from app.claims.drift import DriftContext, Finding, _check_example, detect_drift
from app.claims.extract import Claim, extract_claims
from app.evidence.graph import Graph
from app.llm.client import LLMBudgetExceeded, LLMClient, LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import CriticResult, RewriteResult, SemanticVerdict
from app.pipeline.rewrite import BAD, rewrite

log = logging.getLogger("doctruth.reason")

MAX_REWRITE_ATTEMPTS = 2
MIN_SIMILARITY = 0.5  # an LLM rewrite must stay close to the original text (minimal edit)
BACKTICK = re.compile(r"`([^`\n]+)`")
IDENT = re.compile(r"^[A-Za-z_][\w.]*(?:\(.*\))?$")
PATHLIKE = re.compile(r"^[\w./-]+\.\w{1,5}$")


@dataclass
class EvidenceItem:
    label: str
    kind: str  # code | discussion | history
    ref: str
    text: str
    node_key: tuple | None


def _sym_key(s) -> tuple[str, str]:
    return ("symbol", f"{s.file}:{s.name}")


def _describe(change: dict) -> str:
    return " ".join([change["kind"]] + [f"{k}={v}" for k, v in change.items() if k != "kind" and v not in (None, "", [])])


def evidence_items(f: Finding, graph: Graph) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []

    def add(kind: str, ref: str, text: str, key: tuple | None) -> None:
        items.append(EvidenceItem(f"E{len(items) + 1}", kind, ref, text[:700], key))

    for s in f.symbols:
        key = _sym_key(s)
        data = graph.nodes.get(key, {})
        changes = "; ".join(_describe(c) for c in data.get("changes", []))
        add("code", s.ref, f"{s.kind} {s.name}{s.signature}. Changes in this PR: {changes}", key)
        for src, dst, kind in graph.edges:
            if dst != key or src[0] not in ("comment_thread", "pr", "commit") or kind == "supersedes":
                continue
            node = graph.nodes[src]
            if src[0] == "comment_thread":
                label = "DECISION (resolved review thread)" if node["status"] == "decision" else "OPEN QUESTION, NOT A FACT (unresolved thread)"
                add("discussion", src[1], f"{label}: {node['text']}", src)
            elif src[0] == "pr":
                said = " ".join(st["sentence"] for st in node.get("statements", []))
                add("discussion", src[1], f"PR title: {node.get('title', '')}. {said}", src)
            elif kind == "mentions":
                add("history", src[1][:7], f"commit{' (REVERT)' if node.get('is_revert') else ''}: {node.get('subject', '')}", src)
    for path in (f.file_path, f.meta.get("evidence_file")):
        if path:
            add("history" if path == f.file_path else "code", path, "; ".join(e["note"] for e in f.evidence), ("file", path))
    return items


def render_evidence(items: list[EvidenceItem]) -> str:
    return "\n".join(f"[{i.label}] ({i.kind}) {i.ref}\n{untrusted(i.text)}" for i in items) or "(none)"


def reconcile(f: Finding, graph: Graph) -> None:
    """Fusion rules (master prompt 2.1) as explicit logic, not prompt text."""
    for s in f.symbols:
        node = graph.nodes.get(_sym_key(s), {})
        for c in node.get("contradicted_by", []):
            f.sources_conflict = True
            f.notes.append(f"{c['source']} says {c['claimed']}, merged code says {c['code']}: code wins")
        for src, dst, kind in graph.edges:
            if dst == _sym_key(s) and kind == "mentions" and src[0] == "commit" and graph.nodes[src].get("is_revert"):
                f.confidence = min(f.confidence, 0.6)  # a revert touched this: do not document it without a human
                f.notes.append(f"commit {src[1][:7]} reverts related work in this window")


def reverify(c: Claim, after: str, ctx: DriftContext) -> bool:
    """Re-run the deterministic checks on the rewritten text: a fix that is still wrong is rejected."""
    wrapped = {
        "signature": f"`{after}`", "cli_command": f"`{after}`", "file_path": f"`{after}`", "link": f"[x]({after})",
        "default_value": after, "version_requirement": after,
        "code_example": f"```{c.data.get('lang', '')}\n{after}\n```",
    }.get(c.type)
    if wrapped is None:
        return True
    claims = [n for n in extract_claims(c.doc_path, wrapped) if n.type == c.type]
    if not claims:
        return False
    if c.type in ("file_path", "link") and claims[0].data["path"] not in ctx.tree:
        return False
    found = detect_drift(claims, ctx.changes, ctx.gone, ctx.tree, ctx.manifests)
    return not any(x.verdict in BAD for x in found)


def _known_names(ctx: DriftContext) -> set[str]:
    names: set[str] = set()
    for ch in ctx.changes:
        for sym in (ch.symbol, ch.old):
            if sym:
                names.update(sym.name.replace(" ", ".").split("."))
                names.update(p.name.lstrip("*").rstrip("?") for p in sym.params)
    return names


def _llm_edit_problems(f: Finding, after: str, cited: list[str], items: list[EvidenceItem], ctx: DriftContext) -> list[str]:
    c, problems = f.claim, []
    labels = {i.label for i in items}
    if after.strip() == c.text.strip():
        return ["the rewrite is identical to the original"]
    if not cited or not set(cited) <= labels:
        problems.append("the edit must cite existing evidence labels")
    if difflib.SequenceMatcher(None, c.text, after).ratio() < MIN_SIMILARITY:
        problems.append("the edit is not minimal: keep the original wording")
    known = _known_names(ctx) | set(re.findall(r"[A-Za-z_]\w*", c.text))
    for token in BACKTICK.findall(after):
        if token in c.text:
            continue
        if PATHLIKE.match(token):
            if token not in ctx.tree:
                problems.append(f"path `{token}` does not exist")
        elif IDENT.match(token) and re.match(r"[A-Za-z_][\w.]*", token).group(0).rsplit(".", 1)[-1] not in known:
            problems.append(f"symbol `{token}` is not in the evidence")
    if c.type == "code_example":
        check = _check_example(Claim(c.doc_path, c.heading, c.line_start, c.line_end, c.type, after, c.data),
                               [ch for ch in ctx.changes if ch.symbol])
        if check is not None and check.verdict != "still_true":
            problems.append(f"the example is still wrong: {check.reason}")
    return problems


def _values(f: Finding, items: list[EvidenceItem], **extra: str) -> dict[str, str]:
    c = f.claim
    return {"doc_path": c.doc_path, "line": str(c.line_start), "heading": c.heading, "claim": untrusted(c.text),
            "evidence": render_evidence(items), **extra}


async def _judge(f: Finding, items: list[EvidenceItem], llm: LLMClient, prompts: dict) -> None:
    p = load_prompt("semantic_judge.v1", **_values(f, items))
    v = await llm.complete_json(p.system, p.user, SemanticVerdict, temperature=0.1)
    prompts["semantic_judge"] = p.version
    labels = {i.label for i in items}
    if v.verdict != "unknowable" and not (v.evidence_ids and set(v.evidence_ids) <= labels):
        f.reason = "the model gave a verdict without citing evidence"  # unsupported verdicts are not trusted
        return
    f.verdict, f.confidence, f.reason = v.verdict, v.confidence, v.reason
    f.meta["cited"] = v.evidence_ids


async def _llm_edit(f: Finding, items: list[EvidenceItem], ctx: DriftContext, llm: LLMClient, prompts: dict):
    """Rewrite with the LLM, validate deterministically, then ask a separate critic prompt. Bounded retries."""
    feedback = ""
    for _ in range(MAX_REWRITE_ATTEMPTS):
        p = load_prompt("rewrite.v1", **_values(f, items, problem=f.reason, feedback=feedback))
        rw = await llm.complete_json(p.system, p.user, RewriteResult, temperature=0.2, max_tokens=1500)
        prompts["rewrite"] = p.version
        issues = _llm_edit_problems(f, rw.after, rw.evidence_ids, items, ctx)
        if not issues:
            cp = load_prompt("critic.v1", before=untrusted(f.claim.text), after=untrusted(rw.after), evidence=render_evidence(items))
            verdict = await llm.complete_json(cp.system, cp.user, CriticResult, temperature=0.0)
            prompts["critic"] = cp.version
            if verdict.approved:
                return rw.after, rw.confidence, rw.evidence_ids
            issues = verdict.issues or ["the critic rejected the edit"]
        feedback = "Previous attempt was rejected: " + "; ".join(issues)[:400] + "\n"
    return None


def _question(f: Finding, ev_ids: list[str], why: str = "") -> dict:
    c = f.claim
    return {"kind": "question", "doc_path": c.doc_path, "line_start": c.line_start, "line_end": c.line_end,
            "before": c.text, "after": None, "evidence_ids": ev_ids, "confidence": f.confidence, "method": "none",
            "question": f"`{c.doc_path}` line {c.line_start}: {f.reason}{(' (' + why + ')') if why else ''}. "
                        "Should this part of the docs change?"}


async def _make_edit(f: Finding, ctx: DriftContext, llm: LLMClient | None, items: list[EvidenceItem], ids: dict,
                     threshold: float, prompts: dict) -> None:
    c = f.claim
    keys = [i.node_key for i in items if i.node_key and i.node_key[0] in ("symbol", "file")]
    ev_ids = [str(ids[k]) for k in dict.fromkeys(keys) if k in ids]
    if f.verdict not in BAD or not ev_ids:
        f.edit = _question(f, ev_ids, "" if ev_ids else "no evidence node to cite")
        return
    after = confidence = method = None
    det = rewrite(f)
    if det:
        after, confidence = det
        method = "deterministic"
        if not reverify(c, after, ctx):
            after = None
    elif llm and c.type in ("code_example", "behavior_statement") and "was removed" not in f.reason:
        try:
            got = await _llm_edit(f, items, ctx, llm, prompts)
        except LLMBudgetExceeded:
            got = None
            f.notes.append("LLM call budget reached")
        except LLMError as exc:
            got = None
            f.notes.append(f"LLM unavailable: {exc}"[:200])
        if got:
            after, confidence, cited = got
            method = "llm"
            ev_ids = [str(ids[i.node_key]) for i in items if i.label in cited and i.node_key in ids] or ev_ids
    confidence = min(confidence, f.confidence) if after else 0.0
    if after and confidence >= threshold:
        f.edit = {"kind": "edit", "doc_path": c.doc_path, "line_start": c.line_start, "line_end": c.line_end,
                  "before": c.text, "after": after, "evidence_ids": ev_ids, "confidence": round(confidence, 2),
                  "method": method, "prompt_versions": prompts if method == "llm" else {}}
    else:
        q = _question(f, ev_ids, "low confidence" if after else "")
        if after:
            q["question"] += f" Proposed wording: {after}"
        f.edit = q


async def build_edits(findings: list[Finding], ctx: DriftContext, graph: Graph, ids: dict,
                      llm: LLMClient | None, threshold: float) -> dict:
    for f in findings:
        if f.verdict == "still_true":
            continue
        reconcile(f, graph)
        items = evidence_items(f, graph)
        prompts: dict = {}
        if llm and f.verdict == "unknowable" and f.claim.type == "behavior_statement" and f.symbols:
            try:
                await _judge(f, items, llm, prompts)
            except LLMError as exc:  # the model being down must never fail the run: fall back to asking a human
                f.notes.append(f"LLM unavailable: {exc}"[:200])
        if f.verdict == "still_true":
            continue
        await _make_edit(f, ctx, llm, items, ids, threshold, prompts)
    edits = [f.edit for f in findings if f.edit]
    return {"edits": sum(e["kind"] == "edit" for e in edits), "questions": sum(e["kind"] == "question" for e in edits),
            "llm_calls": llm.calls if llm else 0, "tokens": llm.tokens if llm else 0}
