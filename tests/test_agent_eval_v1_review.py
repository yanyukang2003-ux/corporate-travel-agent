from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
DATASET_ROOT = ROOT / "data" / "evaluation" / "agent-eval-v1"
SCRIPT = ROOT / "examples" / "review_agent_eval_v1.py"


def test_review_files_use_canonical_v2_shape() -> None:
    case_ids = [
        json.loads(line)["case_id"]
        for line in (DATASET_ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    for reviewer in ("reviewer-a", "reviewer-b"):
        review = json.loads(
            (DATASET_ROOT / "reviews" / "round-1" / f"{reviewer}.json").read_text(encoding="utf-8")
        )
        assert review["schema_version"] == 2
        assert review["reviewer"] == reviewer
        assert [item["case_id"] for item in review["records"]] == case_ids
        assert all(
            set(item["human_checks"])
            == {
                "intent_supported",
                "expected_behavior_correct",
                "assertions_sufficient",
            }
            for item in review["records"]
        )


def test_review_cli_renders_independent_comparison_bases() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "show", "core-compliant-round-trip-001"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "【读题（先看这个）】" in result.stdout
    assert "用户原话（证据）" in result.stdout
    assert "用代码当场算" in result.stdout
    assert "REVALIDATION_COVERS_SELECTION" in result.stdout or "机器预检" in result.stdout
    assert "你只需要判断" in result.stdout
    assert "最终应停在哪个状态" in result.stdout


def test_review_cli_audit_has_no_machine_errors_after_dataset_fix() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "audit"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "machine_checks:" in result.stdout
    assert "ERROR=0" in result.stdout
    assert "shown=0" in result.stdout
    # Default audit filter is errors only; remaining WARNs must not appear here.
    assert "WARN " not in result.stdout
    assert not any(line.startswith("ERROR ") for line in result.stdout.splitlines())
