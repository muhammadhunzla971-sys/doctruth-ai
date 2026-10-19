"""Turns Markdown docs into atomic, anchored claims. Pure and deterministic (no LLM)."""
import posixpath
import re
from dataclasses import dataclass, field

FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SIGNATURE_RE = re.compile(r"^([A-Za-z_][\w.]*)\((.*)\)$")
FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)(?:=|\s+)?([^\s`'\"-][^\s`'\"]*)?")
VERSION_RE = re.compile(r"\b(python|node(?:\.js)?)\s*(?:version\s*)?(?:>=|~=|==|=|>|~|\^)?\s*v?(\d+(?:\.\d+)*)\+?", re.I)
DEFAULT_WORD_RE = re.compile(r"default(?:s|ed)?\b", re.I)
DEFAULT_TAIL_RE = re.compile(
    r"\s*(?:value\s+)?(?:(?:of|for)\s+)?(?:[`'\"]?[\w.]+[`'\"]?\s+)?(?:is|to|of|=|:)\s*(?:now\s+)?[`'\"]?([\w.\-]+)", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
PATH_EXTS = {"py", "js", "ts", "tsx", "jsx", "md", "json", "yml", "yaml", "toml", "txt", "ini", "cfg", "sh",
             "env", "html", "css", "sql", "lock", "mjs", "cjs"}
CODE_LANGS = {"python": "python", "py": "python", "javascript": "javascript", "js": "javascript",
              "typescript": "typescript", "ts": "typescript", "tsx": "tsx", "jsx": "javascript"}
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", "powershell", "ps1", "cmd", ""}


@dataclass
class Claim:
    doc_path: str
    heading: str
    line_start: int
    line_end: int
    type: str  # signature|default_value|cli_command|code_example|file_path|link|version_requirement|behavior_statement
    text: str
    data: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.doc_path}:{self.line_start}"


def parse_params(raw: str) -> list[tuple[str, str | None]]:
    """'url, timeout=30, *args' -> [('url', None), ('timeout', '30'), ('*args', None)] (top-level commas only)."""
    parts, depth, cur = [], 0, ""
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    params = []
    for p in (x.strip() for x in parts):
        if not p:
            continue
        name, _, default = p.partition("=")
        name = name.split(":")[0].strip().rstrip("?")
        params.append((name, default.strip().strip("`") if _ else None))
    return params


def _looks_like_path(s: str) -> bool:
    if re.search(r"[\s()=<>*{}]", s) or "://" in s or s.startswith(("-", "$")):
        return False
    ext = s.rsplit(".", 1)[-1].lower() if "." in s else ""
    return ("/" in s and bool(ext)) or ext in PATH_EXTS and "/" not in s and s.count(".") == 1 and not s.startswith(".") \
        or ("/" in s and s.endswith("/"))


def _flags(text: str) -> dict[str, str | None]:
    return {m.group(1): m.group(2) for m in FLAG_RE.finditer(text)}


def extract_claims(doc_path: str, text: str) -> list[Claim]:
    claims: list[Claim] = []
    heading = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line, no = lines[i], i + 1
        fence = FENCE_RE.match(line)
        if fence:
            lang, start, body = fence.group(2).lower(), no, []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence.group(1)):
                body.append(lines[i])
                i += 1
            end = i + 1
            if lang in CODE_LANGS:
                claims.append(Claim(doc_path, heading, start, end, "code_example", "\n".join(body),
                                    {"lang": CODE_LANGS[lang]}))
            elif lang in SHELL_LANGS:
                for k, cmd in enumerate(body):
                    flags = _flags(cmd)
                    if flags:
                        claims.append(Claim(doc_path, heading, start + 1 + k, start + 1 + k, "cli_command",
                                            cmd.strip().lstrip("$ "), {"flags": flags}))
            i += 1
            continue
        h = HEADING_RE.match(line)
        if h:
            heading = h.group(2)
            i += 1
            continue

        for span in CODE_SPAN_RE.findall(line):
            sig = SIGNATURE_RE.match(span.strip())
            if sig:
                claims.append(Claim(doc_path, heading, no, no, "signature", span.strip(),
                                    {"name": sig.group(1), "params": parse_params(sig.group(2))}))
            elif _flags(span):
                claims.append(Claim(doc_path, heading, no, no, "cli_command", span, {"flags": _flags(span)}))
            elif _looks_like_path(span.strip()):
                claims.append(Claim(doc_path, heading, no, no, "file_path", span.strip(), {"path": span.strip().lstrip("./")}))
        for target in LINK_RE.findall(line):
            if not re.match(r"^(?:[a-z]+:|#|//)", target, re.I):
                path = target.split("#", 1)[0]
                if path:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), path))
                    claims.append(Claim(doc_path, heading, no, no, "link", target, {"path": resolved}))

        prose = line.strip()
        if prose and not prose.startswith(("|", "![", "<")):
            for sentence in SENTENCE_RE.split(prose):
                sentence = sentence.strip(" -*>#")
                if len(sentence) < 15:
                    continue
                kind, data = "behavior_statement", {}
                word = DEFAULT_WORD_RE.search(sentence)
                tail = DEFAULT_TAIL_RE.match(sentence, word.end()) if word else None
                version = VERSION_RE.search(sentence)
                if tail:
                    kind, data = "default_value", {"value": tail.group(1).rstrip(".-")}
                elif version:
                    kind, data = "version_requirement", {"runtime": version.group(1).lower().split(".")[0],
                                                         "version": version.group(2)}
                claims.append(Claim(doc_path, heading, no, no, kind, sentence, data))
        i += 1
    return claims
