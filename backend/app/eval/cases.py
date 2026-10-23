"""Labelled benchmark cases. Expectations are written by hand from what a careful human editor would do, never copied from output.

expect: {doc line: ("edit", corrected text of that line/span) | ("question", None)}. A claim not listed must be left alone.
"""
from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    doc: str
    expect: dict
    old: str = ""
    new: str = ""
    path: str = "a.py"
    gone: dict = field(default_factory=dict)
    tree: tuple = ()
    manifests: dict = field(default_factory=dict)


PY = "def fetch(url, timeout=30):\n    return url\n"

CASES = [
    Case("default_keeps_unit", "The default timeout is 30s.\n", {1: ("edit", "The default timeout is 60s.")},
         PY, PY.replace("30", "60")),
    Case("default_spelled_unit", "The default timeout is 30 seconds.\n", {1: ("edit", "The default timeout is 60 seconds.")},
         PY, PY.replace("30", "60")),
    Case("signature_default_changed", "Call `fetch(url, timeout=30)`.\n", {1: ("edit", "fetch(url, timeout=45)")},
         PY, PY.replace("30", "45")),
    Case("signature_param_added", "Call `fetch(url)`.\n", {1: ("edit", "fetch(url, retries=3)")},
         "def fetch(url):\n    return url\n", "def fetch(url, retries=3):\n    return url\n"),
    Case("signature_param_removed", "Call `fetch(url, timeout=30)`.\n", {1: ("edit", "fetch(url)")},
         PY, "def fetch(url):\n    return url\n"),
    Case("function_renamed", "Use `get_user(uid)` to load a user.\n", {1: ("edit", "fetch_user(uid)")},
         "def get_user(uid):\n    row = db.query(uid)\n    return row.name.strip()\n",
         "def fetch_user(uid):\n    row = db.query(uid)\n    return row.name.strip()\n"),
    Case("function_removed_needs_human", "Call `legacy(x)` for old behaviour.\n", {1: ("question", None)},
         "def legacy(x):\n    return x\n\ndef keep(y):\n    return y\n", "def keep(y):\n    return y\n"),
    Case("method_default_changed", "Use `Client.get(path, timeout=30)`.\n", {1: ("edit", "Client.get(path, timeout=60)")},
         "class Client:\n    def get(self, path, timeout=30):\n        return path\n",
         "class Client:\n    def get(self, path, timeout=60):\n        return path\n"),
    Case("js_default_changed", "Call `fetchData(url, timeout=30)`.\n", {1: ("edit", "fetchData(url, timeout=60)")},
         "export function fetchData(url, timeout = 30) {\n  return url;\n}\n",
         "export function fetchData(url, timeout = 60) {\n  return url;\n}\n", path="a.js"),
    Case("cli_default_changed", "```bash\ntool --port 8000\n```\n", {2: ("edit", "tool --port 9000")},
         'import argparse\np = argparse.ArgumentParser()\np.add_argument("--port", default=8000)\n',
         'import argparse\np = argparse.ArgumentParser()\np.add_argument("--port", default=9000)\n'),
    Case("link_to_renamed_file", "See [guide](docs/old.md) for details.\n", {1: ("edit", "docs/new.md")},
         gone={"docs/old.md": "docs/new.md"}, tree=("docs/new.md",)),
    Case("path_removed_needs_human", "Edit `config.yml` first.\n", {1: ("question", None)}, gone={"config.yml": None}),
    Case("python_version_requirement", "Requires Python 3.9+ to run.\n", {1: ("edit", "Requires Python 3.11+ to run.")},
         manifests={"pyproject.toml": '[project]\nrequires-python = ">=3.11"\n'}),
    Case("behavior_claim_asks_not_guesses", "The fetch helper retries on network errors.\n", {1: ("question", None)},
         PY, PY.replace("30", "60")),
    Case("stale_example_asks_not_guesses", "```python\nfetch(url, retries=3)\n```\n", {1: ("question", None)},
         "def fetch(url, retries=3):\n    return url\n", "def fetch(url):\n    return url\n"),
    # --- must stay untouched ---
    Case("already_correct_doc", "The default timeout is 60s.\nCall `fetch(url, timeout=60)`.\n", {},
         PY, PY.replace("30", "60")),
    Case("unrelated_default_left_alone", "The default retries is 3.\n", {}, PY, PY.replace("30", "60")),
    Case("unrelated_function_left_alone", "Call `other(x, timeout=30)`.\n", {}, PY, PY.replace("30", "60")),
    Case("internal_change_only", "Call `fetch(url, timeout=30)`.\n", {},
         PY, "def fetch(url, timeout=30):\n    return str(url)\n"),
    Case("existing_path_not_flagged", "Edit `config.yml` first.\n", {}, tree=("config.yml",)),
]
