"""Offline benchmark: `python -m app.eval.run`. Runs detection + deterministic edits (no network, no LLM) on labelled cases."""
import asyncio
import json
import sys
import uuid
from collections import Counter

from app.claims.drift import DriftContext, detect_drift
from app.claims.extract import extract_claims
from app.eval.cases import CASES, Case
from app.evidence.code_collector import collect_code_changes
from app.evidence.graph import build_graph
from app.evidence.history_collector import HistoryInput, collect_history
from app.evidence.symbols import FileChange
from app.pipeline.reason import build_edits


async def produce(case: Case, llm=None) -> dict[int, tuple[str, str | None]]:
    changes = collect_code_changes([FileChange(case.path, "modified", None, case.old, case.new)]) if case.old or case.new else []
    graph = build_graph(changes, None, collect_history(HistoryInput()), [case.path], "h" * 40)
    ctx = DriftContext(changes, case.gone, set(case.tree), case.manifests)
    findings = detect_drift(extract_claims("README.md", case.doc), changes, ctx.gone, ctx.tree, ctx.manifests)
    ids = {k: uuid.uuid4() for k in graph.nodes}
    for f in findings:  # what pipeline.analyze.register_files does with real nodes
        for path in (f.file_path, f.meta.get("evidence_file")):
            if path:
                ids.setdefault(("file", path), uuid.uuid4())
    await build_edits(findings, ctx, graph, ids, llm, 0.8)
    out = {}
    for f in findings:
        if f.edit:
            out[f.edit["line_start"]] = (f.edit["kind"], f.edit.get("after"))
    return out


def score(case: Case, got: dict) -> Counter:
    c: Counter = Counter()
    for line, (kind, after) in case.expect.items():
        g = got.get(line)
        if kind == "edit":
            c["expected_edits"] += 1
            if g and g[0] == "edit" and g[1] == after:
                c["correct_edits"] += 1
            elif g and g[0] == "edit":
                c["wrong_edits"] += 1
            else:
                c["missed_edits"] += 1
        else:
            c["expected_questions"] += 1
            if g and g[0] == "question":
                c["correct_questions"] += 1
            elif g:
                c["overconfident_edits"] += 1
            else:
                c["missed_questions"] += 1
    for line, (kind, _) in got.items():
        if line not in case.expect:
            c["false_edits" if kind == "edit" else "noise_questions"] += 1
    return c


async def evaluate(cases=CASES, llm=None) -> tuple[Counter, list[dict]]:
    total: Counter = Counter()
    rows = []
    for case in cases:
        got = await produce(case, llm)
        c = score(case, got)
        bad = {k: v for k, v in c.items() if k in ("wrong_edits", "missed_edits", "overconfident_edits", "false_edits",
                                                   "missed_questions", "noise_questions")}
        rows.append({"case": case.id, "ok": not bad, "expected": case.expect, "got": got})
        total += c
    return total, rows


def summarize(t: Counter) -> dict:
    produced = t["correct_edits"] + t["wrong_edits"] + t["overconfident_edits"] + t["false_edits"]
    return {
        "edit_precision": round(t["correct_edits"] / produced, 3) if produced else 1.0,
        "edit_recall": round(t["correct_edits"] / t["expected_edits"], 3) if t["expected_edits"] else 1.0,
        "question_recall": round(t["correct_questions"] / t["expected_questions"], 3) if t["expected_questions"] else 1.0,
        "false_edits": t["false_edits"] + t["overconfident_edits"],
        "wrong_edits": t["wrong_edits"], "noise_questions": t["noise_questions"],
    }


def main() -> int:
    total, rows = asyncio.run(evaluate())
    for r in rows:
        print(("PASS " if r["ok"] else "FAIL ") + r["case"] + ("" if r["ok"] else f"\n     expected {r['expected']}\n     got      {r['got']}"))
    s = summarize(total)
    print("\n" + json.dumps(s, indent=2))
    return 0 if s["false_edits"] == 0 and s["wrong_edits"] == 0 and s["edit_recall"] >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
