"""Deterministic rewrites: the smallest edit that makes a claim true, built from code evidence. No LLM."""
import posixpath
import re

from app.claims.drift import Finding
from app.evidence.symbols import Symbol

SELF = ("self", "cls")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
UNIT_NUMBER = re.compile(r"(\d+(?:\.\d+)?)([A-Za-z%]+)")
BAD = ("contradicted", "incomplete", "stale_example")


def format_params(sym: Symbol) -> str:
    out = []
    for p in sym.params:
        if p.name in SELF:
            continue
        text = p.name + (f": {p.annotation}" if p.annotation else "")
        if p.default is not None:
            text += f" = {p.default}" if p.annotation else f"={p.default}"
        out.append(text)
    return ", ".join(out)


def _value(v: str | None) -> str | None:
    v = (v or "").strip().strip("`'\"")
    return v if v and len(v) <= 40 and " " not in v else None


def _token(old: str) -> re.Pattern:
    return re.compile(rf"(?<![\w.]){re.escape(old)}(?![\w])")


def rewrite(f: Finding) -> tuple[str, float] | None:
    """(after, confidence) or None when the fix cannot be derived mechanically (a question or the LLM takes over)."""
    c = f.claim
    if f.verdict not in BAD or "was removed" in f.reason:
        return None
    after: str | None = None
    confidence = 0.95

    if c.type == "signature" and f.symbols:
        sym = f.symbols[0]
        if f.meta.get("new_name"):
            after = re.sub(r"[A-Za-z_]\w*(?=\()", f.meta["new_name"].rsplit(".", 1)[-1], c.text, count=1)
        elif sym.kind in ("function", "method", "route"):
            after = f"{c.data['name']}({format_params(sym)})"
    elif c.type == "default_value":
        old, new = c.data["value"], _value(f.meta.get("new_value"))
        if new and _token(old).search(c.text):
            unit = UNIT_NUMBER.fullmatch(old)
            if unit and NUMBER.fullmatch(new):
                new, confidence = new + unit.group(2), 0.9  # "30s" -> "60s": the unit is carried over, a mild assumption
            after = _token(old).sub(new, c.text, count=1)
    elif c.type == "cli_command":
        flag, old, new = f.meta.get("flag"), f.meta.get("old_value"), _value(f.meta.get("new_value"))
        if flag and old and new:
            after = re.sub(rf"({re.escape(flag)}(?:=|\s+)){re.escape(old)}", lambda m: m.group(1) + new, c.text, count=1)
    elif c.type in ("file_path", "link") and f.meta.get("new_path") and f.file_path:
        new = f.meta["new_path"]
        if c.type == "file_path" and c.data["path"] == f.file_path:
            after = new
        elif c.type == "link":
            target, _, anchor = c.text.partition("#")
            rel = posixpath.relpath(new, posixpath.dirname(c.doc_path) or ".")
            after = rel + (f"#{anchor}" if anchor else "")
    elif c.type == "version_requirement" and f.meta.get("new_version"):
        after = re.sub(rf"(?<![\d.]){re.escape(f.meta['old_version'])}(?!\d)", f.meta["new_version"], c.text, count=1)

    return (after, confidence) if after and after != c.text else None
