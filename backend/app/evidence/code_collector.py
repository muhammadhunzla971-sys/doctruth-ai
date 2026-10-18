"""Code evidence: tree-sitter symbol extraction and a symbol-level diff between two commits."""
import ast
import hashlib
import re
from functools import lru_cache

import tree_sitter_javascript as ts_js
import tree_sitter_python as ts_py
import tree_sitter_typescript as ts_ts
from tree_sitter import Language, Node, Parser

from app.evidence.symbols import CodeChange, FileChange, Param, Symbol

EXT_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
}
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
CALLABLE_KINDS = {"function", "method", "route"}
CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_FILE_BYTES = 200_000


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    return EXT_LANG.get(path[dot:].lower()) if dot != -1 else None


@lru_cache(maxsize=None)
def _parser(lang: str) -> Parser:
    language = {
        "python": lambda: ts_py.language(),
        "javascript": lambda: ts_js.language(),
        "typescript": lambda: ts_ts.language_typescript(),
        "tsx": lambda: ts_ts.language_tsx(),
    }[lang]()
    return Parser(Language(language))


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", text).encode()).hexdigest()[:16]


def _py_str(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    try:
        value = ast.literal_eval(_t(node))
        return value if isinstance(value, str) else None
    except (ValueError, SyntaxError):
        return None


def _js_str(node: Node | None) -> str | None:
    if node is None or node.type not in ("string", "template_string"):
        return None
    return _t(node)[1:-1]


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _flag_name(strings: list[str]) -> str | None:
    """'-p, --port <n>' (commander) or ['-p', '--port'] (argparse/click) -> '--port'."""
    tokens = [tok for s in strings for tok in re.split(r"[\s,|]+", s) if tok.startswith("-")]
    longs = [t for t in tokens if t.startswith("--")]
    return (longs or tokens or [None])[0]


# ---------------------------------------------------------------- Python

def _py_params(node: Node | None) -> tuple[Param, ...]:
    params = []
    for c in (node.named_children if node else []):
        if c.type == "comment":
            continue
        if c.type == "identifier":
            params.append(Param(_t(c)))
        elif c.type in ("default_parameter", "typed_parameter", "typed_default_parameter"):
            name = c.child_by_field_name("name") or c.named_children[0]
            value = c.child_by_field_name("value")
            params.append(Param(_t(name), _norm(_t(c.child_by_field_name("type"))),
                                _norm(_t(value)) if value is not None else None))
        else:  # *args, **kwargs, bare * and /
            params.append(Param(_norm(_t(c))))
    return tuple(params)


def _py_callable(fn: Node, path: str, name: str, kind: str, exported: bool) -> Symbol:
    body = fn.child_by_field_name("body")
    body_text = _t(body)
    return Symbol(
        kind=kind, name=name, file=path, line=fn.start_point[0] + 1, end_line=fn.end_point[0] + 1,
        params=_py_params(fn.child_by_field_name("parameters")),
        returns=_norm(_t(fn.child_by_field_name("return_type"))),
        body_hash=_hash(body_text), body_len=len(body_text), exported=exported,
    )


def _py_routes(decorators: list[Node], handler: Symbol) -> list[Symbol]:
    routes = []
    for dec in decorators:
        call = next((c for c in dec.named_children if c.type == "call"), None)
        func = call.child_by_field_name("function") if call else None
        if func is None or func.type != "attribute":
            continue
        verb = _t(func.child_by_field_name("attribute")).lower()
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args and args.named_children else None
        path = _py_str(first)
        if path is None or (verb not in HTTP_METHODS and verb != "route"):
            continue
        methods = [verb.upper()]
        if verb == "route":
            found = re.search(r"methods\s*=\s*\[([^\]]*)\]", _t(args))
            methods = re.findall(r"['\"](\w+)['\"]", found.group(1)) if found else ["GET"]
        for m in methods:
            routes.append(Symbol(kind="route", name=f"{m.upper()} {path}", file=handler.file, line=handler.line,
                                 end_line=handler.end_line, params=handler.params, returns=handler.returns,
                                 body_hash=handler.body_hash, body_len=handler.body_len))
    return routes


def _py_symbols(root: Node, path: str) -> list[Symbol]:
    out: list[Symbol] = []

    def visit(block: Node, prefix: str, in_class: bool) -> None:
        for child in block.named_children:
            node, decorators = child, []
            if child.type == "decorated_definition":
                decorators = [c for c in child.named_children if c.type == "decorator"]
                node = child.child_by_field_name("definition") or child
            if node.type == "function_definition":
                name = _t(node.child_by_field_name("name"))
                if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
                    continue
                sym = _py_callable(node, path, prefix + name, "method" if in_class else "function", True)
                out.append(sym)
                out.extend(_py_routes(decorators, sym))
            elif node.type == "class_definition":
                name = _t(node.child_by_field_name("name"))
                if name.startswith("_"):
                    continue
                body = node.child_by_field_name("body")
                out.append(Symbol(kind="class", name=prefix + name, file=path, line=node.start_point[0] + 1,
                                  end_line=node.end_point[0] + 1, body_hash=_hash(_t(body)), body_len=len(_t(body))))
                if body is not None:
                    visit(body, f"{prefix}{name}.", True)
            elif node.type == "expression_statement" and not in_class and node.named_children:
                a = node.named_children[0]
                left = a.child_by_field_name("left") if a.type == "assignment" else None
                if left is not None and left.type == "identifier" and CONST_RE.match(_t(left)):
                    value = _norm(_t(a.child_by_field_name("right")))[:200]
                    out.append(Symbol(kind="constant", name=_t(left), file=path, line=a.start_point[0] + 1,
                                      end_line=a.end_point[0] + 1, value=value))

    visit(root, "", False)

    for node in _walk(root):
        if node.type == "call":
            fn = _t(node.child_by_field_name("function"))
            args_node = node.child_by_field_name("arguments")
            args = args_node.named_children if args_node else []
            kwargs = {_t(a.child_by_field_name("name")): _norm(_t(a.child_by_field_name("value")))
                      for a in args if a.type == "keyword_argument"}
            line = node.start_point[0] + 1
            if fn in ("os.environ.get", "os.getenv", "environ.get", "getenv") and args:
                var = _py_str(args[0])
                if var:
                    default = _norm(_t(args[1])) if len(args) > 1 and args[1].type != "keyword_argument" else kwargs.get("default")
                    out.append(Symbol(kind="env_var", name=var, file=path, line=line, end_line=line, value=default))
            elif fn.endswith(".add_argument") or fn in ("click.option", "option"):
                flag = _flag_name([s for s in (_py_str(a) for a in args) if s])
                if flag:
                    out.append(Symbol(kind="cli_flag", name=flag, file=path, line=line, end_line=line,
                                      value=kwargs.get("default")))
        elif node.type == "subscript" and _t(node.child_by_field_name("value")) in ("os.environ", "environ"):
            var = _py_str(node.child_by_field_name("subscript"))
            if var:
                line = node.start_point[0] + 1
                out.append(Symbol(kind="env_var", name=var, file=path, line=line, end_line=line))
    return out


# ------------------------------------------------------- JavaScript / TypeScript

def _js_params(node: Node | None) -> tuple[Param, ...]:
    params = []
    for c in (node.named_children if node else []):
        if c.type == "comment":
            continue
        if c.type == "assignment_pattern":
            params.append(Param(_t(c.child_by_field_name("left")), "", _norm(_t(c.child_by_field_name("right")))))
        elif c.type in ("required_parameter", "optional_parameter"):
            name = _t(c.child_by_field_name("pattern")) + ("?" if c.type == "optional_parameter" else "")
            ann = _norm(_t(c.child_by_field_name("type"))).lstrip(": ")
            value = c.child_by_field_name("value")
            params.append(Param(name, ann, _norm(_t(value)) if value is not None else None))
        else:
            params.append(Param(_norm(_t(c))))
    return tuple(params)


def _js_callable(fn: Node, path: str, name: str, kind: str, exported: bool) -> Symbol:
    body = fn.child_by_field_name("body")
    body_text = _t(body)
    return Symbol(
        kind=kind, name=name, file=path, line=fn.start_point[0] + 1, end_line=fn.end_point[0] + 1,
        params=_js_params(fn.child_by_field_name("parameters")),
        returns=_norm(_t(fn.child_by_field_name("return_type"))).lstrip(": "),
        body_hash=_hash(body_text), body_len=len(body_text), exported=exported,
    )


def _js_symbols(root: Node, path: str) -> list[Symbol]:
    out: list[Symbol] = []

    def declare(node: Node, exported: bool) -> None:
        if node.type in ("function_declaration", "generator_function_declaration"):
            out.append(_js_callable(node, path, _t(node.child_by_field_name("name")), "function", exported))
        elif node.type == "class_declaration":
            name = _t(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            out.append(Symbol(kind="class", name=name, file=path, line=node.start_point[0] + 1,
                              end_line=node.end_point[0] + 1, body_hash=_hash(_t(body)), body_len=len(_t(body)),
                              exported=exported))
            for m in (body.named_children if body else []):
                if m.type == "method_definition":
                    mname = _t(m.child_by_field_name("name"))
                    if not mname.startswith("#") and not mname.startswith("_"):
                        out.append(_js_callable(m, path, f"{name}.{mname}", "method", exported))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type != "variable_declarator":
                    continue
                name_node, value = d.child_by_field_name("name"), d.child_by_field_name("value")
                if name_node is None or value is None or name_node.type != "identifier":
                    continue
                name = _t(name_node)
                if value.type in ("arrow_function", "function_expression", "function"):
                    out.append(_js_callable(value, path, name, "function", exported))
                elif CONST_RE.match(name):
                    out.append(Symbol(kind="constant", name=name, file=path, line=d.start_point[0] + 1,
                                      end_line=d.end_point[0] + 1, value=_norm(_t(value))[:200], exported=exported))

    for top in root.named_children:
        if top.type == "export_statement":
            decl = top.child_by_field_name("declaration")
            if decl is not None:
                declare(decl, True)
        else:
            declare(top, False)

    for node in _walk(root):
        line = node.start_point[0] + 1
        if node.type == "member_expression" and _t(node.child_by_field_name("object")) == "process.env":
            out.append(Symbol(kind="env_var", name=_t(node.child_by_field_name("property")), file=path,
                              line=line, end_line=line))
        elif node.type == "subscript_expression" and _t(node.child_by_field_name("object")) == "process.env":
            var = _js_str(node.child_by_field_name("index"))
            if var:
                out.append(Symbol(kind="env_var", name=var, file=path, line=line, end_line=line))
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")
            args = args_node.named_children if args_node else []
            if fn is None or fn.type != "member_expression" or not args:
                continue
            verb = _t(fn.child_by_field_name("property"))
            first = _js_str(args[0])
            if verb in HTTP_METHODS and first and first.startswith("/"):
                handler = next((a for a in reversed(args) if a.type in ("arrow_function", "function_expression", "function")), None)
                body_text = _t(handler.child_by_field_name("body")) if handler is not None else ""
                out.append(Symbol(
                    kind="route", name=f"{verb.upper()} {first}", file=path, line=line, end_line=node.end_point[0] + 1,
                    params=_js_params(handler.child_by_field_name("parameters")) if handler is not None else (),
                    body_hash=_hash(body_text), body_len=len(body_text)))
            elif verb == "option" and first and first.startswith("-"):
                flag = _flag_name([first])
                default = _norm(_t(args[2])) if len(args) > 2 and args[2].type not in ("arrow_function", "function_expression") else None
                if flag:
                    out.append(Symbol(kind="cli_flag", name=flag, file=path, line=line, end_line=line, value=default))
    return out


# ---------------------------------------------------------------- public API

def extract_symbols(path: str, text: str) -> list[Symbol]:
    lang = language_for(path)
    if lang is None or not text or len(text.encode()) > MAX_FILE_BYTES:
        return []
    root = _parser(lang).parse(text.encode()).root_node
    symbols = _py_symbols(root, path) if lang == "python" else _js_symbols(root, path)
    unique: dict[tuple, Symbol] = {}
    for s in symbols:
        unique.setdefault(s.key, s)  # first occurrence wins (e.g. an env var read in several places)
    return list(unique.values())


def _diff_callable(old: Symbol, new: Symbol) -> list[CodeChange]:
    changes: list[CodeChange] = []
    old_by, new_by = {p.name: p for p in old.params}, {p.name: p for p in new.params}
    shape = lambda s: ([(p.name, p.annotation) for p in s.params], s.returns)  # noqa: E731
    if shape(old) != shape(new):
        changes.append(CodeChange("signature_changed", new, old, {
            "old": old.signature, "new": new.signature,
            "added_params": [n for n in new_by if n not in old_by],
            "removed_params": [n for n in old_by if n not in new_by],
        }))
    for name, p in new_by.items():
        if name in old_by and old_by[name].default != p.default:
            changes.append(CodeChange("default_changed", new, old, {"param": name, "old": old_by[name].default, "new": p.default}))
    if not changes and old.body_hash != new.body_hash and new.kind in CALLABLE_KINDS:
        changes.append(CodeChange("body_changed", new, old))
    return changes


def diff_symbols(old: list[Symbol], new: list[Symbol]) -> list[CodeChange]:
    old_by, new_by = {s.key: s for s in old}, {s.key: s for s in new}
    changes: list[CodeChange] = []
    for key, n in new_by.items():
        o = old_by.get(key)
        if o is None:
            continue
        if n.kind in CALLABLE_KINDS:
            changes.extend(_diff_callable(o, n))
        elif o.value != n.value:
            kind = "value_changed" if n.kind == "constant" else "default_changed"
            changes.append(CodeChange(kind, n, o, {"param": "value" if n.kind == "constant" else "default",
                                                   "old": o.value, "new": n.value}))
    removed = [s for k, s in old_by.items() if k not in new_by]
    added = [s for k, s in new_by.items() if k not in old_by]
    for r in list(removed):
        match = next((a for a in added if a.kind == r.kind and a.kind in (CALLABLE_KINDS | {"class"})
                      and r.body_hash and a.body_hash == r.body_hash and a.body_len >= 20), None)
        if match:
            added.remove(match)
            removed.remove(r)
            changes.append(CodeChange("renamed", match, r, {"old_name": r.name, "new_name": match.name,
                                                            "old_file": r.file, "new_file": match.file}))
    changes += [CodeChange("removed", s) for s in removed]
    changes += [CodeChange("added", s) for s in added]
    return changes


def collect_code_changes(files: list[FileChange]) -> list[CodeChange]:
    """Compare symbols between the old and new text of every changed file."""
    old_syms: list[Symbol] = []
    new_syms: list[Symbol] = []
    changes: list[CodeChange] = []
    for f in files:
        if language_for(f.path) is None:
            continue
        old_path = f.old_path or f.path
        old = extract_symbols(old_path, f.old_text or "") if f.status != "added" else []
        for s in old:
            s.file = f.path  # a renamed file keeps its symbol keys, so only real symbol changes show up
        old_syms += old
        new_syms += extract_symbols(f.path, f.new_text or "") if f.status != "removed" else []
        if f.status == "renamed" and f.old_path and f.old_path != f.path:
            changes.append(CodeChange("file_renamed", detail={"old_path": f.old_path, "new_path": f.path}))
    return changes + diff_symbols(old_syms, new_syms)
