"""History evidence: commits in range, reverts, renames and the latest release."""
import re
from dataclasses import dataclass, field

REVERT_SUBJECT_RE = re.compile(r'^Revert "(?P<subject>.+)"', re.I)
REVERT_SHA_RE = re.compile(r"This reverts commit (?P<sha>[0-9a-f]{7,40})", re.I)


@dataclass
class CommitInfo:
    sha: str
    message: str

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0][:200]


@dataclass
class Revert:
    commit: str
    reverted_sha: str | None
    reverted_subject: str | None


@dataclass
class HistoryInput:
    commits: list[CommitInfo] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)  # (old_path, new_path)
    latest_release: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class HistoryEvidence:
    commits: list[CommitInfo]
    reverts: list[Revert]
    renames: list[tuple[str, str]]
    latest_release: str | None
    tags: list[str]


def collect_history(data: HistoryInput) -> HistoryEvidence:
    reverts = []
    for c in data.commits:
        subject = REVERT_SUBJECT_RE.match(c.message)
        sha = REVERT_SHA_RE.search(c.message)
        if subject or sha:
            reverts.append(Revert(c.sha, sha.group("sha") if sha else None, subject.group("subject") if subject else None))
    return HistoryEvidence(data.commits, reverts, data.renames, data.latest_release, data.tags)
