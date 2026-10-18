from app.evidence.code_collector import collect_code_changes, extract_symbols
from app.evidence.symbols import FileChange

OLD_PY = '''
import os
RETRY_COUNT = 3
TIMEOUT = 30
def fetch(url, retries=3):
    return os.environ.get("API_KEY", "x")
class Client:
    def get(self, path, timeout=30):
        return do_the_thing_with(path, timeout)
    def old_name(self, a):
        total = a + 1
        return total * 2 + 99999
def _private(a=1):
    return a
@app.get("/users")
def users(limit: int = 10):
    return []
parser.add_argument("--port", default=8000)
'''
NEW_PY = '''
import os
RETRY_COUNT = 5
def fetch(url, retries=5, verbose=False):
    return os.environ.get("API_KEY", "y")
class Client:
    def get(self, path, timeout=60):
        return do_the_thing_with(path, timeout)
    def new_name(self, a):
        total = a + 1
        return total * 2 + 99999
def _private(a=2):
    return a
@app.get("/users")
def users(limit: int = 10, offset: int = 0):
    return []
parser.add_argument("--port", default=9000)
'''


def changes_for(old, new, path="a.py", status="modified"):
    return collect_code_changes([FileChange(path, status, None, old, new)])


def find(changes, kind, name):
    return next((c for c in changes if c.kind == kind and c.symbol and c.symbol.name == name), None)


def test_python_symbol_extraction():
    syms = {(s.kind, s.name): s for s in extract_symbols("a.py", OLD_PY)}
    assert ("function", "fetch") in syms and ("method", "Client.get") in syms and ("class", "Client") in syms
    assert ("constant", "RETRY_COUNT") in syms and ("route", "GET /users") in syms
    assert ("env_var", "API_KEY") in syms and ("cli_flag", "--port") in syms
    assert ("function", "_private") not in syms  # private names are not public surface
    assert syms[("function", "fetch")].signature == "(url, retries=3)"
    assert syms[("route", "GET /users")].signature == "(limit: int = 10)"


def test_python_changes():
    ch = changes_for(OLD_PY, NEW_PY)
    assert find(ch, "value_changed", "RETRY_COUNT").detail["new"] == "5"
    d = find(ch, "default_changed", "fetch").detail
    assert (d["param"], d["old"], d["new"]) == ("retries", "3", "5")
    assert find(ch, "signature_changed", "fetch").detail["added_params"] == ["verbose"]
    assert find(ch, "default_changed", "Client.get").detail["new"] == "60"
    assert find(ch, "signature_changed", "GET /users").detail["added_params"] == ["offset"]
    assert find(ch, "default_changed", "API_KEY").detail["new"] == '"y"'
    assert find(ch, "default_changed", "--port").detail["new"] == "9000"
    assert find(ch, "removed", "TIMEOUT")
    renamed = find(ch, "renamed", "Client.new_name")
    assert renamed.detail["old_name"] == "Client.old_name"
    assert not any(c.symbol and c.symbol.name == "_private" for c in ch)


def test_only_default_change_is_not_a_signature_change():
    ch = changes_for("def f(a, b=1):\n    return a\n", "def f(a, b=2):\n    return a\n")
    assert [c.kind for c in ch] == ["default_changed"]


def test_body_change_is_reported_when_signature_is_same():
    ch = changes_for("def f(a):\n    return a + 1\n", "def f(a):\n    return a + 2\n")
    assert [c.kind for c in ch] == ["body_changed"]


def test_added_and_removed_files():
    added = changes_for(None, "def f():\n    return 1\n", status="added")
    assert [c.kind for c in added] == ["added"]
    removed = changes_for("def f():\n    return 1\n", None, status="removed")
    assert [c.kind for c in removed] == ["removed"]


def test_renamed_file_does_not_report_every_symbol_as_new():
    src = "def f(a):\n    return a\n"
    ch = collect_code_changes([FileChange("new/a.py", "renamed", "old/a.py", src, src)])
    assert [c.kind for c in ch] == ["file_renamed"]
    assert ch[0].detail == {"old_path": "old/a.py", "new_path": "new/a.py"}


def test_flask_route_with_methods_and_click_option():
    src = '''
@app.route("/x", methods=["GET", "POST"])
def x():
    return 1
@click.option("-p", "--port", default=80)
def main(port):
    pass
'''
    names = {(s.kind, s.name) for s in extract_symbols("a.py", src)}
    assert {("route", "GET /x"), ("route", "POST /x"), ("cli_flag", "--port")} <= names


def test_syntax_garbage_does_not_crash():
    assert isinstance(extract_symbols("a.py", "def (((:\n"), list)
    assert extract_symbols("notes.md", "# hi") == []


OLD_TS = '''
export function f(a, b: number = 3) { return process.env.API_URL; }
export const MAX_RETRIES = 3;
export class Svc { run(x = 1) { return x; } }
app.get("/items", (req, res) => { res.send([]); });
program.option("-p, --port <n>", "port", 80);
'''
NEW_TS = '''
export function f(a, b: number = 5, c?: string) { return process.env.API_URL; }
export const MAX_RETRIES = 4;
export class Svc { run(x = 2) { return x; } }
app.get("/items", (req, res) => { res.send([1]); });
program.option("-p, --port <n>", "port", 8080);
'''


def test_typescript_symbols_and_changes():
    syms = {(s.kind, s.name) for s in extract_symbols("a.ts", OLD_TS)}
    assert {("function", "f"), ("constant", "MAX_RETRIES"), ("class", "Svc"), ("method", "Svc.run"),
            ("route", "GET /items"), ("cli_flag", "--port"), ("env_var", "API_URL")} <= syms
    ch = changes_for(OLD_TS, NEW_TS, "a.ts")
    assert find(ch, "default_changed", "f").detail["new"] == "5"
    assert find(ch, "signature_changed", "f").detail["added_params"] == ["c?"]
    assert find(ch, "value_changed", "MAX_RETRIES").detail["new"] == "4"
    assert find(ch, "default_changed", "Svc.run")
    assert find(ch, "body_changed", "GET /items")
    assert find(ch, "default_changed", "--port").detail["new"] == "8080"


def test_javascript_arrow_function_and_tsx():
    ch = changes_for("export const g = (x = 1) => x;\n", "export const g = (x = 2) => x;\n", "a.js")
    assert find(ch, "default_changed", "g").detail["new"] == "2"
    assert extract_symbols("c.tsx", "export function C({a}: P) { return <div/>; }\n")
