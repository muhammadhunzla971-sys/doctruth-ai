from app.claims.drift import detect_drift
from app.claims.extract import extract_claims, parse_params
from app.evidence.code_collector import collect_code_changes
from app.evidence.symbols import FileChange

DOC = '''# Guide

## Retries
The default timeout is 30s. RETRY_COUNT default is 3.
Call `fetch(url, timeout=30)` and `gone(a)`. Also `old.py` exists, see [x](docs/old.md).
Run `tool --port 8000`. Requires Python 3.11+.

```python
fetch("u", retries=2)
```

```bash
$ tool --port 8000 --verbose
```
'''
OLD = "RETRY_COUNT = 3\ndef fetch(url, timeout=30):\n    return url\ndef gone(a):\n    return a + 1 + 22222222222\nparser.add_argument('--port', default=8000)\n"
NEW = "RETRY_COUNT = 5\ndef fetch(url, timeout=60, verbose=False):\n    return url\nparser.add_argument('--port', default=9000)\n"


def drift(doc=DOC, old=OLD, new=NEW, gone=None, tree=(), manifests=None):
    changes = collect_code_changes([FileChange("a.py", "modified", None, old, new)])
    return detect_drift(extract_claims("README.md", doc), changes, gone or {}, set(tree), manifests or {})


def by_type(findings, type_):
    return [f for f in findings if f.claim.type == type_]


def test_parse_params_handles_defaults_types_and_nesting():
    assert parse_params("url, timeout=30, *args, opts: dict = {'a': 1, 'b': 2}") == [
        ("url", None), ("timeout", "30"), ("*args", None), ("opts", "{'a': 1, 'b': 2}")]


def test_extractor_finds_each_claim_type_with_anchors():
    claims = extract_claims("README.md", DOC)
    types = {c.type for c in claims}
    assert {"default_value", "signature", "file_path", "link", "cli_command", "code_example",
            "version_requirement", "behavior_statement"} <= types
    sig = next(c for c in claims if c.type == "signature" and c.data["name"] == "fetch")
    assert sig.heading == "Retries" and sig.line_start == 5 and sig.data["params"] == [("url", None), ("timeout", "30")]
    assert next(c for c in claims if c.type == "link").data["path"] == "docs/old.md"
    assert next(c for c in claims if c.type == "code_example").data["lang"] == "python"


def test_default_and_constant_claims_are_contradicted_by_code():
    found = by_type(drift(), "default_value")
    assert {f.verdict for f in found} == {"contradicted"} and len(found) == 2
    assert any("30s" in f.reason and "60" in f.reason for f in found)
    assert all(f.evidence[0]["kind"] == "code" for f in found)


def test_signature_removed_symbol_and_incomplete_signature():
    found = {f.claim.data["name"]: f for f in by_type(drift(), "signature")}
    assert found["gone"].verdict == "contradicted" and "removed" in found["gone"].reason
    assert found["fetch"].verdict == "contradicted"
    doc = "Use `fetch(url, timeout=60)` here.\n"
    assert drift(doc)[0].verdict == "incomplete"
    assert drift("Use `fetch(url, timeout=60, verbose=False)`.\n")[0].verdict == "still_true"


def test_removed_and_renamed_paths_and_links():
    found = drift(gone={"old.py": "new.py", "docs/old.md": None}, tree={"new.py", "a.py"})
    paths = {f.claim.type: f for f in found if f.claim.type in ("file_path", "link")}
    assert "renamed to `new.py`" in paths["file_path"].reason and paths["file_path"].file_path == "old.py"
    assert "removed" in paths["link"].reason


def test_path_that_still_exists_in_the_tree_is_not_flagged():
    assert not [f for f in drift(gone={"old.py": None}, tree={"old.py"}) if f.claim.type == "file_path"]


def test_cli_flag_default_change_is_flagged():
    f = by_type(drift(), "cli_command")
    assert f and all(x.verdict == "contradicted" and "9000" in x.reason for x in f)


def test_stale_example_uses_removed_parameter_but_unrelated_examples_are_ignored():
    assert by_type(drift(), "code_example")[0].verdict == "stale_example"
    assert not by_type(drift("```python\nprint('hi')\n```\n"), "code_example")
    broken = "Use `fetch`:\n```python\nfetch(url,, timeout=)\n```\n"
    assert by_type(drift(broken), "code_example")[0].reason == "example does not parse"


def test_version_requirement_checked_only_when_manifest_changed():
    assert not by_type(drift(), "version_requirement")
    f = by_type(drift(manifests={"pyproject.toml": 'requires-python = ">=3.10"'}), "version_requirement")[0]
    assert f.verdict == "contradicted" and "3.10" in f.reason
    ok = by_type(drift(manifests={"pyproject.toml": 'requires-python = ">=3.11"'}), "version_requirement")[0]
    assert ok.verdict == "still_true"


def test_vague_behavior_sentence_is_unknowable_and_not_duplicated():
    doc = "The fetch helper retries on network errors and then gives up.\nCall `fetch(url, timeout=30)`.\n"
    found = drift(doc)
    assert [f.claim.type for f in found].count("signature") == 1
    behavior = by_type(found, "behavior_statement")
    assert behavior and behavior[0].verdict == "unknowable" and behavior[0].claim.line_start == 1
    assert not [f for f in found if f.claim.line_start == 2 and f.claim.type == "behavior_statement"]


def test_untouched_docs_produce_no_findings():
    assert drift("Nothing related here, just some prose about other things.\n") == []
