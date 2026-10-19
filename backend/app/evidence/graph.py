"""Unified evidence graph: fuses the three collectors into nodes and edges (fusion rules, Section 2.1)."""
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence.discussion_collector import DiscussionEvidence, Statement
from app.evidence.history_collector import HistoryEvidence
from app.evidence.symbols import CodeChange, Symbol
from app.models import EvidenceEdge, EvidenceNode

MAX_CHANGES = 300
DEFAULT_WORD_RE = re.compile(r"default(?:s|ed)?\b", re.I)
# after the word "default":  [value] [of|for] [<name>] (is|to|of|=|:) [now] <VALUE>
DEFAULT_TAIL_RE = re.compile(
    r"\s*(?:value\s+)?(?:(?:of|for)\s+)?(?:[`'\"]?[\w.]+[`'\"]?\s+)?(?:is|to|of|=|:)\s*(?:now\s+)?[`'\"]?([\w.\-]+)", re.I)
ASSIGN_RE = r"\W{0,3}(?:=|is|to|set to)\s*[`'\"]?([\w.\-]+)"
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?")

Key = tuple[str, str]  # (node kind, ref)


@dataclass
class Graph:
    nodes: dict[Key, dict] = field(default_factory=dict)
    edges: list[tuple[Key, Key, str]] = field(default_factory=list)

    def node(self, kind: str, ref: str, **data) -> Key:
        key = (kind, ref)
        self.nodes.setdefault(key, {}).update(data)
        return key

    def edge(self, src: Key, dst: Key, kind: str) -> None:
        if (src, dst, kind) not in self.edges:
            self.edges.append((src, dst, kind))


def _mention_tokens(sym: Symbol) -> list[str]:
    name = sym.name.split(" ", 1)[1] if sym.kind == "route" else sym.name
    tokens = [name]
    last = name.rsplit(".", 1)[-1]
    if last != name and (len(last) >= 6 or "_" in last):
        tokens.append(last)
    return [t for t in tokens if len(t) >= 4 or any(c in t for c in "_./-")]


def _mentions(text: str, tokens: list[str]) -> bool:
    return any(re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", text, re.I) for t in tokens)


def _norm_value(v: str | None) -> str:
    return (v or "").strip().strip("`'\"").rstrip(".,;").lower()


def same_value(a: str | None, b: str | None) -> bool:
    na, nb = _norm_value(a), _norm_value(b)
    ma, mb = NUMBER_RE.match(na), NUMBER_RE.match(nb)
    if ma and mb:
        return float(ma.group()) == float(mb.group())
    return na == nb


def claimed_value(sentence: str, tokens: list[str]) -> str | None:
    """What a sentence says the default/value of one of `tokens` is, if it says so."""
    if not _mentions(sentence, tokens):
        return None
    for word in DEFAULT_WORD_RE.finditer(sentence):
        m = DEFAULT_TAIL_RE.match(sentence, word.end())
        if m:
            return m.group(1).rstrip(".-")
    for t in tokens:
        m = re.search(rf"(?<![\w]){re.escape(t)}(?![\w]){ASSIGN_RE}", sentence, re.I)
        if m:
            return m.group(1).rstrip(".-")
    return None


def _code_value(change: CodeChange) -> tuple[list[str], str | None] | None:
    """(names a sentence could use, current value) for changes where discussion can contradict code."""
    sym = change.symbol
    if sym is None:
        return None
    if change.kind == "default_changed" and sym.kind in ("function", "method", "route"):
        return [change.detail["param"], sym.name], change.detail["new"]
    if change.kind in ("default_changed", "value_changed"):
        return [sym.name], change.detail["new"]
    return None


def build_graph(
    changes: list[CodeChange],
    discussion: DiscussionEvidence | None,
    history: HistoryEvidence,
    changed_files: list[str],
    head_sha: str,
) -> Graph:
    g = Graph()
    anchor = g.node("pr", f"PR#{discussion.pr_number}") if discussion and discussion.pr_number else g.node("commit", head_sha)
    if discussion and discussion.pr_number:
        g.node("pr", f"PR#{discussion.pr_number}", title=discussion.title, linked_issues=discussion.linked_issues,
               statements=[s.__dict__ for s in discussion.pr_statements])
    else:
        g.node("commit", head_sha, role="head")

    for path in changed_files:
        g.edge(anchor, g.node("file", path), "modifies")

    symbol_keys: list[tuple[Key, Symbol]] = []
    for ch in changes[:MAX_CHANGES]:
        if ch.kind == "file_renamed":
            g.edge(g.node("file", ch.detail["new_path"]), g.node("file", ch.detail["old_path"]), "renamed_from")
            continue
        sym = ch.symbol
        key = g.node("symbol", f"{sym.file}:{sym.name}", symbol_kind=sym.kind, name=sym.name, location=sym.ref,
                     signature=sym.signature, value=sym.value)
        entry = {"kind": ch.kind, **{k: v for k, v in ch.detail.items() if k not in ("old_file", "new_file")}}
        g.nodes[key].setdefault("changes", []).append(entry)
        g.edge(anchor, key, "modifies")
        if ch.kind == "renamed" and ch.old is not None:
            old_key = g.node("symbol", f"{ch.old.file}:{ch.old.name}", symbol_kind=ch.old.kind, name=ch.old.name,
                             location=ch.old.ref, signature=ch.old.signature, removed=True)
            g.edge(key, old_key, "renamed_from")
        if not any(k == key for k, _ in symbol_keys):
            symbol_keys.append((key, sym))

    # history: commits that matter (reverts, or touching symbols we found) and revert -> reverted edges
    commit_keys: dict[str, Key] = {}
    for c in history.commits:
        syms = [(k, s) for k, s in symbol_keys if _mentions(c.message, _mention_tokens(s))]
        is_revert = any(r.commit == c.sha for r in history.reverts)
        if syms or is_revert:
            ck = g.node("commit", c.sha, subject=c.subject)
            commit_keys[c.sha] = ck
            for k, _ in syms:
                g.edge(ck, k, "mentions")
    for r in history.reverts:
        target = next((c for c in history.commits
                       if (r.reverted_sha and c.sha.startswith(r.reverted_sha[:7]))
                       or (r.reverted_subject and c.subject == r.reverted_subject)), None)
        if target is not None:
            dst = g.node("commit", target.sha, subject=target.subject)
        elif r.reverted_sha:
            dst = g.node("commit", r.reverted_sha, outside_range=True)
        else:
            continue
        g.edge(commit_keys[r.commit], dst, "supersedes")
        g.nodes[commit_keys[r.commit]]["is_revert"] = True

    # discussion: mentions, justifications and contradictions
    if discussion:
        pr_key = ("pr", f"PR#{discussion.pr_number}") if discussion.pr_number else None
        contradictions: list[tuple[Key, Key, dict]] = []

        def check_contradictions(src: Key, statements: list[Statement]) -> None:
            for sentence in (s.sentence for s in statements if s.signal == "default"):
                for ch in changes[:MAX_CHANGES]:
                    cv = _code_value(ch)
                    if cv is None:
                        continue
                    names, value = cv
                    said = claimed_value(sentence, names)
                    if said is not None and value is not None and not same_value(said, value):
                        contradictions.append((src, g.node("symbol", f"{ch.symbol.file}:{ch.symbol.name}"),
                                               {"claimed": said, "code": value, "sentence": sentence, "source": src[1]}))

        if pr_key:
            for key, sym in symbol_keys:
                if _mentions(f"{discussion.title}\n{discussion.body}", _mention_tokens(sym)):
                    g.edge(pr_key, key, "mentions")
            check_contradictions(pr_key, discussion.pr_statements)
        for t in discussion.threads:
            ref = f"PR#{discussion.pr_number}/thread-{t.index}"
            tk = g.node("comment_thread", ref, status=t.status, path=t.path, line=t.line, text=t.text[:600],
                        statements=[s.__dict__ for s in t.statements])
            for key, sym in symbol_keys:
                on_symbol = t.path == sym.file and t.line is not None and sym.line <= t.line <= sym.end_line
                if on_symbol or _mentions(t.text, _mention_tokens(sym)):
                    g.edge(tk, key, "justifies" if t.status == "decision" else "mentions")
            if t.status == "decision":  # unresolved threads are open questions, never facts
                check_contradictions(tk, t.statements)
        for src, dst, info in contradictions:
            g.edge(src, dst, "contradicts")
            g.nodes[dst].setdefault("contradicted_by", []).append(info)
    return g


async def persist_graph(session: AsyncSession, run_id: uuid.UUID, graph: Graph) -> dict[Key, uuid.UUID]:
    """Nodes first, then edges: the ORM does not order inserts across tables without relationship()."""
    ids = {key: uuid.uuid4() for key in graph.nodes}
    session.add_all(EvidenceNode(id=ids[key], run_id=run_id, kind=key[0], ref=key[1][:500], data=data)
                    for key, data in graph.nodes.items())
    await session.flush()
    session.add_all(EvidenceEdge(run_id=run_id, src_id=ids[src], dst_id=ids[dst], kind=kind)
                    for src, dst, kind in graph.edges)
    await session.flush()
    return ids
