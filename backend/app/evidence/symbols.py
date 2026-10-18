"""Plain data types shared by the code collector and the graph builder."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    name: str
    annotation: str = ""
    default: str | None = None


@dataclass
class Symbol:
    kind: str  # function | method | class | constant | route | env_var | cli_flag
    name: str  # qualified: "Client.get", "RETRY_COUNT", "GET /users", "API_KEY", "--port"
    file: str
    line: int
    end_line: int
    params: tuple[Param, ...] = ()
    returns: str = ""
    value: str | None = None  # constant value, or default of an env var / CLI flag
    body_hash: str = ""
    body_len: int = 0
    exported: bool = True

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.name, self.file)

    @property
    def ref(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def signature(self) -> str:
        parts = []
        for p in self.params:
            text = p.name + (f": {p.annotation}" if p.annotation else "")
            if p.default is not None:
                text += f" = {p.default}" if p.annotation else f"={p.default}"
            parts.append(text)
        sig = f"({', '.join(parts)})"
        return sig + (f" -> {self.returns}" if self.returns else "")


@dataclass
class FileChange:
    path: str
    status: str  # added | modified | removed | renamed
    old_path: str | None = None
    old_text: str | None = None
    new_text: str | None = None


@dataclass
class CodeChange:
    # added | removed | renamed | signature_changed | default_changed | value_changed | body_changed | file_renamed
    kind: str
    symbol: Symbol | None = None  # the symbol as it is now (or as it was, for "removed")
    old: Symbol | None = None
    detail: dict = field(default_factory=dict)
