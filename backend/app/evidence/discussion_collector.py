"""Discussion evidence: PR text, review threads, linked issues and commit messages.

Everything here is untrusted data. It is only classified and quoted, never executed or obeyed.
"""
import re
from dataclasses import dataclass, field

MAX_TEXT = 2000

SIGNALS: dict[str, re.Pattern] = {
    "deprecation": re.compile(r"deprecat|sunset|will be removed|remove[d]? in (?:the )?next", re.I),
    "breaking": re.compile(r"breaking change|backwards?[- ]incompatible|not backwards?[- ]compatible", re.I),
    "revert": re.compile(r"\brevert(?:s|ed|ing)?\b", re.I),
    "rename": re.compile(r"\brenam(?:e|es|ed|ing)\b", re.I),
    "rejected": re.compile(r"instead of|rejected|decided not to|won't do|will not do|out of scope", re.I),
    "caveat": re.compile(r"\bnote that\b|\bcaveat\b|\bonly when\b|\bonly if\b|\bmake sure\b|\bbe careful\b", re.I),
    "default": re.compile(r"\bdefault(?:s|ed)?\b", re.I),
}
CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s+#(\d+)", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class Comment:
    author: str
    body: str


@dataclass
class Thread:
    path: str | None
    line: int | None
    resolved: bool
    comments: list[Comment]
    outdated: bool = False


@dataclass
class DiscussionInput:
    pr_number: int | None = None
    title: str = ""
    body: str = ""
    threads: list[Thread] = field(default_factory=list)
    commit_messages: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)  # {"number", "title", "body"}


@dataclass
class Statement:
    signal: str
    sentence: str


@dataclass
class ThreadEvidence:
    index: int
    path: str | None
    line: int | None
    status: str  # "decision" (resolved) | "open_question" (unresolved: must never be written as fact)
    text: str
    statements: list[Statement]


@dataclass
class DiscussionEvidence:
    pr_number: int | None
    title: str
    body: str
    pr_statements: list[Statement]
    threads: list[ThreadEvidence]
    commit_statements: list[Statement]
    linked_issues: list[dict]


def clip(text: str | None) -> str:
    return (text or "")[:MAX_TEXT]


def extract_statements(text: str) -> list[Statement]:
    found: list[Statement] = []
    for sentence in SENTENCE_RE.split(clip(text)):
        sentence = sentence.strip()
        if not sentence:
            continue
        for signal, pattern in SIGNALS.items():
            if pattern.search(sentence):
                found.append(Statement(signal, sentence[:300]))
    return found


def linked_issue_numbers(text: str) -> list[int]:
    return sorted({int(n) for n in CLOSING_RE.findall(clip(text))})


def collect_discussion(data: DiscussionInput) -> DiscussionEvidence:
    threads = []
    for i, t in enumerate(data.threads, start=1):
        text = clip("\n".join(f"{c.author}: {c.body}" for c in t.comments))
        threads.append(ThreadEvidence(
            index=i, path=t.path, line=t.line, status="decision" if t.resolved else "open_question",
            text=text, statements=extract_statements(text),
        ))
    commit_statements = [s for m in data.commit_messages for s in extract_statements(m)]
    return DiscussionEvidence(
        pr_number=data.pr_number, title=clip(data.title), body=clip(data.body),
        pr_statements=extract_statements(f"{data.title}\n{data.body}"),
        threads=threads, commit_statements=commit_statements,
        linked_issues=[{"number": i["number"], "title": clip(i.get("title"))[:200]} for i in data.issues],
    )
