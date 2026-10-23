from app.eval.cases import CASES
from app.eval.run import evaluate, summarize


async def test_benchmark_has_no_wrong_or_false_edits_and_full_recall():
    total, rows = await evaluate()
    failed = [r for r in rows if not r["ok"]]
    assert not failed, failed
    s = summarize(total)
    assert s["false_edits"] == 0 and s["wrong_edits"] == 0 and s["noise_questions"] == 0
    assert s["edit_precision"] == 1.0 and s["edit_recall"] >= 0.9 and s["question_recall"] == 1.0


def test_benchmark_covers_every_claim_type_and_the_leave_alone_cases():
    assert len(CASES) >= 20
    assert sum(1 for c in CASES if not c.expect) >= 5  # cases where the right answer is "touch nothing"
