"""Recount released verdicts; this does not re-review model answers."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWED = {"correct", "partial", "wrong", "unanswered"}


def main():
    with (ROOT / "data/review-matrix.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    summary = json.loads((ROOT / "data/benchmark-summary.json").read_text(encoding="utf-8"))
    primary = [row for row in rows if row["diagnostic"] == "false"]
    assert len(rows) == 14 and len(primary) == 13
    recounted = {}
    for system, expected in summary["abc_fixed_expansion"]["systems"].items():
        reviewed = [row for row in primary if row[system] in REVIEWED]
        correct = sum(row[system] == "correct" for row in reviewed)
        assert correct == expected["correct"]
        assert len(reviewed) == expected["reviewed_primary_tasks"]
        recounted[system] = {"correct": correct, "reviewed_primary_tasks": len(reviewed)}
    disagreement = [row for row in primary if row["A_additional_review"]]
    assert len(disagreement) == 1 and disagreement[0]["sample_id"] == "MATH10"
    alternative = sum((row["A_additional_review"] or row["A"]) == "correct" for row in primary)
    assert alternative == summary["abc_fixed_expansion"]["disagreement"]["alternative_correct_count"]
    print(json.dumps({"recounted": recounted, "A_if_additional_review_adopted": alternative,
                      "raw_answer_review_reproduced": False}, indent=2))


if __name__ == "__main__":
    main()
