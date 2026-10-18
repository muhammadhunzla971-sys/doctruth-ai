from sqlalchemy import select

from app.evidence.code_collector import collect_code_changes
from app.evidence.discussion_collector import Comment, DiscussionInput, Thread, collect_discussion
from app.evidence.graph import build_graph, claimed_value, persist_graph, same_value
from app.evidence.history_collector import CommitInfo, HistoryInput, collect_history
from app.evidence.symbols import FileChange
from app.models import EvidenceEdge, EvidenceNode, Run

OLD = "RETRY_COUNT = 3\ndef fetch(url, timeout=30):\n    return url\n"
NEW = "RETRY_COUNT = 5\ndef fetch(url, timeout=60):\n    return url\n"
EMPTY_HISTORY = collect_history(HistoryInput())


def edges_of(g, kind):
    return [(s, d) for s, d, k in g.edges if k == kind]


def graph_for(discussion=None, history=EMPTY_HISTORY, old=OLD, new=NEW):
    changes = collect_code_changes([FileChange("a.py", "modified", None, old, new)])
    return build_graph(changes, discussion, history, ["a.py"], "h" * 40)


def test_same_value_is_number_and_quote_aware():
    assert same_value("30s", "30") and same_value("`5`", "5.0") and same_value('"x"', "x")
    assert not same_value("30s", "60")


def test_claimed_value_reads_default_statements():
    assert claimed_value("The default timeout is 30s.", ["timeout"]) == "30s"
    assert claimed_value("RETRY_COUNT is 3 now", ["RETRY_COUNT"]) == "3"
    assert claimed_value("Unrelated default is 9", ["timeout"]) is None


def test_code_changes_become_symbol_nodes_modified_by_the_head_commit():
    g = graph_for()
    assert ("symbol", "a.py:RETRY_COUNT") in g.nodes and ("file", "a.py") in g.nodes
    anchor = ("commit", "h" * 40)
    assert (anchor, ("symbol", "a.py:fetch")) in edges_of(g, "modifies")


def test_pr_claim_that_code_contradicts_is_flagged_and_code_wins():
    d = collect_discussion(DiscussionInput(pr_number=9, title="Retry tweaks", body="The default timeout is 30s."))
    g = graph_for(d)
    assert (("pr", "PR#9"), ("symbol", "a.py:fetch")) in edges_of(g, "contradicts")
    info = g.nodes[("symbol", "a.py:fetch")]["contradicted_by"][0]
    assert info["claimed"] == "30s" and info["code"] == "60"


def test_matching_claim_is_not_a_contradiction():
    d = collect_discussion(DiscussionInput(pr_number=9, title="t", body="The default timeout is now 60."))
    assert edges_of(graph_for(d), "contradicts") == []


def test_resolved_thread_justifies_and_unresolved_only_mentions():
    d = collect_discussion(DiscussionInput(pr_number=9, title="t", body="", threads=[
        Thread("a.py", 2, True, [Comment("ann", "Doubling the timeout because of incidents.")]),
        Thread("a.py", 2, False, [Comment("bob", "Maybe the default timeout is 10?")]),
    ]))
    g = graph_for(d)
    fetch = ("symbol", "a.py:fetch")
    assert (("comment_thread", "PR#9/thread-1"), fetch) in edges_of(g, "justifies")
    assert (("comment_thread", "PR#9/thread-2"), fetch) in edges_of(g, "mentions")
    assert g.nodes[("comment_thread", "PR#9/thread-2")]["status"] == "open_question"
    assert edges_of(g, "contradicts") == []  # an open question is never treated as a fact


def test_revert_supersedes_reverted_commit_and_rename_links_symbols():
    h = collect_history(HistoryInput(commits=[
        CommitInfo("a" * 40, 'Revert "Add fetch"\n\nThis reverts commit ' + "b" * 40),
        CommitInfo("b" * 40, "Add fetch"),
    ]))
    g = graph_for(history=h)
    assert ((("commit", "a" * 40)), ("commit", "b" * 40)) in edges_of(g, "supersedes")
    old = "def old_name(a):\n    total = a + 1\n    return total * 2 + 99999\n"
    new = old.replace("old_name", "new_name")
    r = graph_for(old=old, new=new)
    assert (("symbol", "a.py:new_name"), ("symbol", "a.py:old_name")) in edges_of(r, "renamed_from")


async def test_persist_graph_writes_nodes_and_edges(maker):
    d = collect_discussion(DiscussionInput(pr_number=9, title="t", body="The default timeout is 30s."))
    g = graph_for(d)
    async with maker() as s:
        run = Run(installation_id=1, repo_full_name="a/b", kind="preview")
        s.add(run)
        await s.flush()
        ids = await persist_graph(s, run.id, g)
        await s.commit()
        nodes = list(await s.scalars(select(EvidenceNode)))
        edges = list(await s.scalars(select(EvidenceEdge)))
    assert len(nodes) == len(g.nodes) == len(ids) and len(edges) == len(g.edges)
    assert {n.kind for n in nodes} >= {"pr", "file", "symbol"}
