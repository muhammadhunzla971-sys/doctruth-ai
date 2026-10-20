from app.claims.drift import Finding
from app.claims.extract import Claim
from app.evidence.symbols import Param, Symbol
from app.pipeline.rewrite import format_params, rewrite


def claim(type_, text, **data):
    return Claim("docs/guide.md", "H", 3, 3, type_, text, data)


def finding(c, reason="x", meta=None, symbols=None, file_path=None, verdict="contradicted"):
    return Finding(c, verdict, reason, symbols=symbols or [], meta=meta or {}, file_path=file_path)


def sym(name="fetch", kind="function", params=()):
    return Symbol(kind=kind, name=name, file="a.py", line=1, end_line=2, params=tuple(params))


def test_default_value_keeps_the_unit_and_only_changes_the_number():
    f = finding(claim("default_value", "The default timeout is 30s, see below.", value="30s"), meta={"new_value": "60"})
    assert rewrite(f) == ("The default timeout is 60s, see below.", 0.9)
    plain = finding(claim("default_value", "RETRY_COUNT default is 3.", value="3"), meta={"new_value": "5"})
    assert rewrite(plain) == ("RETRY_COUNT default is 5.", 0.95)
    quoted = finding(claim("default_value", "The default mode is fast.", value="fast"), meta={"new_value": '"slow"'})
    assert rewrite(quoted)[0] == "The default mode is slow."


def test_signature_rewrite_uses_the_current_parameters_without_self():
    s = sym("Client.get", "method", [Param("self"), Param("path"), Param("timeout", "", "60"), Param("n", "int", "1")])
    assert format_params(s) == "path, timeout=60, n: int = 1"
    f = finding(claim("signature", "Client.get(path, timeout=30)", name="Client.get", params=[]), symbols=[s])
    assert rewrite(f)[0] == "Client.get(path, timeout=60, n: int = 1)"


def test_renamed_function_keeps_the_documented_arguments():
    f = finding(claim("signature", "Client.old_name(a)", name="Client.old_name", params=[]),
                meta={"new_name": "Client.new_name"}, symbols=[sym("Client.new_name", "method")])
    assert rewrite(f)[0] == "Client.new_name(a)"


def test_removed_things_and_classes_are_left_to_a_human():
    removed = finding(claim("signature", "gone(a)", name="gone", params=[]), reason="`gone` was removed", symbols=[sym("gone")])
    assert rewrite(removed) is None
    cls = finding(claim("signature", "Client(a)", name="Client", params=[]), symbols=[sym("Client", "class")])
    assert rewrite(cls) is None
    assert rewrite(finding(claim("signature", "f(a)", name="f", params=[]), verdict="still_true", symbols=[sym("f")])) is None


def test_cli_flag_default_is_updated_in_place():
    f = finding(claim("cli_command", "tool --port 8000 --verbose", flags={}), meta={"flag": "--port", "old_value": "8000", "new_value": "9000"})
    assert rewrite(f)[0] == "tool --port 9000 --verbose"


def test_renamed_file_and_link_targets():
    f = finding(claim("file_path", "old.py", path="old.py"), meta={"new_path": "new.py"}, file_path="old.py")
    assert rewrite(f)[0] == "new.py"
    link = Claim("docs/guide.md", "H", 3, 3, "link", "old.md#intro", {"path": "docs/old.md"})
    assert rewrite(finding(link, meta={"new_path": "docs/new.md"}, file_path="docs/old.md"))[0] == "new.md#intro"
    gone = finding(claim("file_path", "old.py", path="old.py"), reason="`old.py` was removed", meta={"new_path": None}, file_path="old.py")
    assert rewrite(gone) is None


def test_version_requirement():
    f = finding(claim("version_requirement", "Requires Python 3.11+.", runtime="python", version="3.11"),
                meta={"old_version": "3.11", "new_version": "3.10"})
    assert rewrite(f)[0] == "Requires Python 3.10+."
