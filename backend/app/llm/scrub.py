"""Secret scrubbing: runs on every message before it leaves for the LLM provider."""
import re

PLACEHOLDER = "[REDACTED]"
PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWT
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s]+", re.I),  # URLs with credentials
]
# name = value / name: value where the name says it is a secret: keep the name, drop the value
ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)[A-Z0-9_.-]*)(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"
)


def scrub(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    return ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}", text)
