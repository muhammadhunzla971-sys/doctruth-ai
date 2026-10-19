"""Deterministic drift detection: checks doc claims against the evidence of one change. No LLM."""
import json
import posixpath
import re
from dataclasses import dataclass, field

from app.claims.extract import Claim
from app.evidence.code_collector import _parser, _t, _walk
from app.evidence.graph import _mention_tokens, _mentions, same_value
from app.evidence.symbols import CodeChange, Symbol

CONF = 0.95  # deterministic checks against merged code


@dataclass
class Finding:
    claim: Claim
    verdict: str  # still_true | contradicted | stale_example | incomplete | unknowable
    reason: str
    confidence: float = CONF
    evidence: list[dict] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    file_path: str | None = None  # path-type evidence node (removed/renamed file)


def _last(name: str) -> str:
    return name.rsplit(".", 1)[-1].split(" ")[-1]


def _tokens(ch: CodeChange) -> list[str]:
    toks = _mention_tokens(ch.symbol) if ch.symbol else []
    if ch.kind == "renamed" and ch.old:
        toks += _mention_tokens(ch.old)
    return toks


def _code_ev(ch: CodeChange, note: str) -> dict:
    sym = ch.old if ch.kind == "renamed" and ch.old else ch.symbol
    return {"kind": "code", "ref": sym.ref, "note": note}


def _claimed_params(claim: Claim) -> list[tuple[str, str | None]]:
    return [p for p in claim.data["params"] if p[0] not in ("self", "cls")]


def _check_signature(claim: Claim, changes: list[CodeChange]) -> Finding | None:
    name = _last(claim.data["name"])
    for ch in changes:
        if not ch.symbol or ch.symbol.kind not in ("function", "method", "class", "route"):
            continue
        old_name = ch.old.name if ch.kind == "renamed" and ch.old else ch.symbol.name
        if _last(old_name) != name:
            continue
        if ch.kind == "removed":
            return Finding(claim, "contradicted", f"`{name}` was removed", evidence=[_code_ev(ch, "symbol removed")], symbols=[ch.symbol])
        if ch.kind == "renamed":
            return Finding(claim, "contradicted", f"`{old_name}` was renamed to `{ch.symbol.name}`",
                           evidence=[_code_ev(ch, f"renamed to {ch.symbol.name}")], symbols=[ch.symbol])
        if ch.kind in ("signature_changed", "default_changed"):
            new = [(p.name, p.default) for p in ch.symbol.params if p.name not in ("self", "cls")]
            claimed = _claimed_params(claim)
            same_prefix = [c[0] for c in claimed] == [n[0] for n in new[: len(claimed)]]
            defaults_ok = all(c[1] is None or same_value(c[1], n[1]) for c, n in zip(claimed, new))
            ev = [_code_ev(ch, f"signature is now {ch.symbol.signature}")]
            if same_prefix and defaults_ok and len(claimed) == len(new):
                return Finding(claim, "still_true", "signature matches the code", evidence=ev, symbols=[ch.symbol])
            if same_prefix and defaults_ok and len(claimed) < len(new):
                return Finding(claim, "incomplete", "new parameters are not documented", evidence=ev, symbols=[ch.symbol])
            return Finding(claim, "contradicted", f"signature is now `{name}{ch.symbol.signature}`", evidence=ev, symbols=[ch.symbol])
    return None


def _check_default(claim: Claim, changes: list[CodeChange]) -> Finding | None:
    from app.evidence.graph import claimed_value  # local import keeps the module graph simple

    for ch in changes:
        if not ch.symbol:
            continue
        if ch.kind == "removed":
            if claimed_value(claim.text, [ch.symbol.name]) is not None:
                return Finding(claim, "contradicted", f"`{ch.symbol.name}` was removed",
                               evidence=[_code_ev(ch, "symbol removed")], symbols=[ch.symbol])
            continue
        if ch.kind not in ("default_changed", "value_changed"):
            continue
        names = [ch.detail["param"], ch.symbol.name] if ch.symbol.kind in ("function", "method", "route") else [ch.symbol.name]
        said = claimed_value(claim.text, names)
        if said is None or ch.detail["new"] is None:
            continue
        ev = [_code_ev(ch, f"{ch.detail['param']} is now {ch.detail['new']} (was {ch.detail['old']})")]
        if same_value(said, ch.detail["new"]):
            return Finding(claim, "still_true", "value matches the code", evidence=ev, symbols=[ch.symbol])
        return Finding(claim, "contradicted", f"docs say {said}, code says {ch.detail['new']}", evidence=ev, symbols=[ch.symbol])
    return None


def _check_cli(claim: Claim, changes: list[CodeChange]) -> Finding | None:
    result = None
    for flag, value in claim.data["flags"].items():
        for ch in changes:
            if not ch.symbol or ch.symbol.kind != "cli_flag" or ch.symbol.name != flag:
                continue
            ev = [_code_ev(ch, f"flag {flag}: {ch.kind}")]
            if ch.kind == "removed":
                return Finding(claim, "contradicted", f"flag `{flag}` was removed", evidence=ev, symbols=[ch.symbol])
            if ch.kind == "default_changed" and value is not None and ch.detail["new"] is not None \
                    and not same_value(value, ch.detail["new"]):
                return Finding(claim, "contradicted", f"docs say {flag} {value}, default is now {ch.detail['new']}",
                               evidence=ev, symbols=[ch.symbol])
            result = Finding(claim, "still_true", "flag still matches", evidence=ev, symbols=[ch.symbol])
    return result


def _check_path(claim: Claim, path: str, gone: dict[str, str | None], tree: set[str]) -> Finding | None:
    for old, new in gone.items():
        if (old == path or old.endswith("/" + path)) and old not in tree:
            reason = f"`{path}` was renamed to `{new}`" if new else f"`{path}` was removed"
            return Finding(claim, "contradicted", reason, file_path=old,
                           evidence=[{"kind": "history", "ref": old, "note": reason}])
    return None


def python_calls(code: str):
    """(function name, positional count, keyword names, has *args/**kwargs) for each call in a Python snippet."""
    root = _parser("python").parse(code.encode()).root_node
    for node in _walk(root):
        if node.type != "call":
            continue
        args = node.child_by_field_name("arguments")
        pos, kws, star = 0, [], False
        for a in (args.named_children if args else []):
            if a.type == "keyword_argument":
                kws.append(_t(a.child_by_field_name("name")))
            elif a.type in ("list_splat", "dictionary_splat"):
                star = True
            elif a.type != "comment":
                pos += 1
        yield _t(node.child_by_field_name("function")), pos, kws, star


def _check_example(claim: Claim, changes: list[CodeChange]) -> Finding | None:
    lang, code = claim.data["lang"], claim.text
    related = [ch for ch in changes if ch.symbol and _mentions(code, _tokens(ch))]
    if not related:
        return None
    parser = _parser(lang)
    if parser.parse(code.encode()).root_node.has_error:
        return Finding(claim, "stale_example", "example does not parse", symbols=[ch.symbol for ch in related])
    if lang == "python":
        for fn, pos, kws, star in python_calls(code):
            for ch in related:
                old_name = ch.old.name if ch.kind == "renamed" and ch.old else ch.symbol.name
                if _last(old_name) != _last(fn):
                    continue
                ev = [_code_ev(ch, ch.kind)]
                if ch.kind == "removed":
                    return Finding(claim, "stale_example", f"example calls removed `{fn}`", evidence=ev, symbols=[ch.symbol])
                if ch.kind == "renamed":
                    return Finding(claim, "stale_example", f"`{fn}` was renamed to `{ch.symbol.name}`", evidence=ev, symbols=[ch.symbol])
                if ch.kind in ("signature_changed", "default_changed"):
                    names = [p.name for p in ch.symbol.params if p.name not in ("self", "cls")]
                    var = any(p.name.startswith("*") for p in ch.symbol.params)
                    bad = [k for k in kws if k not in names] if not var else []
                    if bad or (pos > len(names) and not var):
                        return Finding(claim, "stale_example",
                                       f"example uses {', '.join(bad) or 'too many arguments'}; signature is now {ch.symbol.signature}",
                                       evidence=[_code_ev(ch, f"signature is now {ch.symbol.signature}")], symbols=[ch.symbol])
    return Finding(claim, "still_true", "example parses and matches current signatures", symbols=[ch.symbol for ch in related])


def _manifest_version(runtime: str, manifests: dict[str, str]) -> str | None:
    if runtime == "python" and "pyproject.toml" in manifests:
        m = re.search(r"requires-python\s*=\s*[\"'][^\d]*(\d+(?:\.\d+)*)", manifests["pyproject.toml"])
        return m.group(1) if m else None
    if runtime == "node" and "package.json" in manifests:
        try:
            engines = json.loads(manifests["package.json"]).get("engines", {}).get("node", "")
        except ValueError:
            return None
        m = re.search(r"(\d+(?:\.\d+)*)", engines)
        return m.group(1) if m else None
    return None


def _check_version(claim: Claim, manifests: dict[str, str]) -> Finding | None:
    actual = _manifest_version(claim.data["runtime"], manifests)
    if actual is None:
        return None
    wanted = claim.data["version"]
    ok = actual.split(".")[: len(wanted.split("."))] == wanted.split(".")
    file = "pyproject.toml" if claim.data["runtime"] == "python" else "package.json"
    return Finding(claim, "still_true" if ok else "contradicted",
                   "version matches the manifest" if ok else f"{file} now requires {actual}, docs say {wanted}",
                   evidence=[{"kind": "code", "ref": file, "note": f"requires {actual}"}])


def detect_drift(
    claims: list[Claim],
    changes: list[CodeChange],
    gone_paths: dict[str, str | None],
    tree_paths: set[str],
    manifests: dict[str, str],
) -> list[Finding]:
    """Only claims related to something this change touched are evaluated (the doc-to-code scope)."""
    real = [c for c in changes if c.symbol is not None]
    findings: list[Finding] = []
    for claim in claims:
        f: Finding | None = None
        if claim.type == "signature":
            f = _check_signature(claim, real)
        elif claim.type == "default_value":
            f = _check_default(claim, real)
        elif claim.type == "cli_command":
            f = _check_cli(claim, real)
        elif claim.type == "file_path":
            f = _check_path(claim, claim.data["path"], gone_paths, tree_paths)
        elif claim.type == "link":
            f = _check_path(claim, claim.data["path"], gone_paths, tree_paths)
        elif claim.type == "code_example":
            f = _check_example(claim, real)
        elif claim.type == "version_requirement":
            f = _check_version(claim, manifests)
        elif claim.type == "behavior_statement":
            related = [ch for ch in real if ch.kind != "added" and _mentions(claim.text, _tokens(ch))]
            if related:
                f = Finding(claim, "unknowable", "semantic statement about a changed symbol; needs the LLM check (Phase 4)",
                            confidence=0.0, evidence=[_code_ev(related[0], related[0].kind)], symbols=[related[0].symbol])
        if f is not None:
            findings.append(f)
    # a prose sentence already judged by a precise check is not reported again as a vague behavior claim
    judged = {(f.claim.doc_path, f.claim.line_start) for f in findings if f.claim.type != "behavior_statement"}
    return [f for f in findings if f.claim.type != "behavior_statement" or (f.claim.doc_path, f.claim.line_start) not in judged]
