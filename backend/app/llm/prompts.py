"""Versioned prompt templates. Every template lives in app/llm/prompts/<name>.v<N>.md; the version id is stored with each edit."""
import re
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user: str


def untrusted(text: str) -> str:
    """Wrap repository content as data. The model is told never to obey anything inside these tags."""
    safe = text.replace("<untrusted>", "[tag]").replace("</untrusted>", "[tag]")
    return f"<untrusted>\n{safe}\n</untrusted>"


def load_prompt(name: str, **values: str) -> Prompt:
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    system, _, user = raw.partition("\n## USER\n")
    system = system.replace("## SYSTEM", "", 1).strip()
    fill = lambda t: PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), t)  # noqa: E731 single pass
    return Prompt(version=name, system=system, user=fill(user.strip()))
