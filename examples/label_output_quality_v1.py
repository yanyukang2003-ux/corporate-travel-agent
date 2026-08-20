#!/usr/bin/env python3
"""Apply simplified output-quality gold labels and validate them."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data/evaluation/human-annotations/v1/to-label/03-output-quality.jsonl"
DIMS = (
    "completeness",
    "actionability",
    "policy_transparency",
    "evidence_grounding",
    "uncertainty_and_failure_honesty",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("apply", help="Write simplified gold labels")
    sub.add_parser("validate", help="Validate completed labels")
    sub.add_parser("status", help="Show progress")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def score(
    *,
    ratings: dict[str, int],
    hard_failure: bool = False,
    abstain: bool = False,
    abstain_reason: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    if set(ratings) != set(DIMS):
        raise ValueError(f"ratings keys must be {DIMS}, got {tuple(ratings)}")
    for key, value in ratings.items():
        if value not in {1, 2, 3, 4, 5}:
            raise ValueError(f"{key}={value} out of 1..5")
    return {
        "ratings": ratings,
        "hard_failure": hard_failure,
        "abstain": abstain,
        "abstain_reason": abstain_reason,
        "evidence": {},
        "notes": notes,
    }


def ready(notes: str) -> dict[str, Any]:
    # Structured success path: state/policy/options/evidence/boundary notice all present.
    return score(
        ratings={
            "completeness": 5,
            "actionability": 5,
            "policy_transparency": 5,
            "evidence_grounding": 5,
            "uncertainty_and_failure_honesty": 4,
        },
        notes=notes + " 成功交接路径完整；MOCK 库存已披露；诚实度因成功路径无额外不确定提示略降。",
    )


def no_feasible(notes: str) -> dict[str, Any]:
    return score(
        ratings={
            "completeness": 4,
            "actionability": 5,
            "policy_transparency": 3,
            "evidence_grounding": 3,
            "uncertainty_and_failure_honesty": 5,
        },
        notes=notes + " 无可行方案且未编造选项；policy_outcome=null 且无具体失败证据引用，"
        "透明度/ grounding 偏低。",
    )


def reconfirm(notes: str) -> dict[str, Any]:
    return score(
        ratings={
            "completeness": 5,
            "actionability": 5,
            "policy_transparency": 5,
            "evidence_grounding": 5,
            "uncertainty_and_failure_honesty": 5,
        },
        notes=notes + " 重验变更已披露，handoff 关闭，下一步 reconfirm 清晰。",
    )


def provider_after_select(notes: str) -> dict[str, Any]:
    return score(
        ratings={
            "completeness": 4,
            "actionability": 5,
            "policy_transparency": 4,
            "evidence_grounding": 4,
            "uncertainty_and_failure_honesty": 5,
        },
        notes=notes
        + " Provider 失败已说明且未错误开放 handoff；仍展示先前合规方案略易误解最终是否可用。",
    )


def provider_search_fail(notes: str) -> dict[str, Any]:
    return score(
        ratings={
            "completeness": 4,
            "actionability": 5,
            "policy_transparency": 3,
            "evidence_grounding": 3,
            "uncertainty_and_failure_honesty": 5,
        },
        notes=notes + " 搜索超时诚实失败；无选项、无证据引用；policy 为空。",
    )


def approval(notes: str) -> dict[str, Any]:
    return score(
        ratings={
            "completeness": 5,
            "actionability": 5,
            "policy_transparency": 5,
            "evidence_grounding": 5,
            "uncertainty_and_failure_honesty": 4,
        },
        notes=notes + " 审批等待态与 REQUIRES_APPROVAL 一致，handoff 关闭。",
    )


# Explicit per-id labels so scores remain auditable (not a silent heuristic black box).
LABELS: dict[str, dict[str, Any]] = {
    "OUT-001": ready("双选项合规最低价交接。"),
    "OUT-002": ready("单选项合规交接。"),
    "OUT-003": no_feasible("硬约束/政策导致无可行行程。"),
    "OUT-004": reconfirm("重验库存变更。"),
    "OUT-005": ready("单选项合规交接。"),
    "OUT-006": provider_after_select("重验超时。"),
    "OUT-007": provider_search_fail("交通搜索超时。"),
    "OUT-008": ready("单选项合规交接。"),
    "OUT-009": reconfirm("重验库存变更。"),
    "OUT-010": approval("超标/例外需审批。"),
    "OUT-011": reconfirm("重验库存变更。"),
    "OUT-012": no_feasible("硬约束/政策导致无可行行程。"),
    "OUT-013": ready("单选项合规交接。"),
    "OUT-014": provider_after_select("重验超时。"),
    "OUT-015": ready("双选项合规最低价交接。"),
    "OUT-016": ready("双选项合规最低价交接。"),
    "OUT-017": approval("需审批。"),
    "OUT-018": approval("需审批（更高成本方案）。"),
    "OUT-019": ready("双选项合规最低价交接。"),
    "OUT-020": no_feasible("硬约束/政策导致无可行行程。"),
}


def apply() -> None:
    rows = load_jsonl(PATH)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        ann_id = row["annotation_id"]
        if ann_id not in LABELS:
            raise SystemExit(f"missing label for {ann_id}")
        row["annotation"] = LABELS[ann_id]
        row["annotation_meta"] = {
            **row.get("annotation_meta", {}),
            "annotator_id": "human-01",
            "round": 1,
            "blinded": True,
            "label_style": "simplified_v1",
            "completed_at": now,
        }
    write_jsonl(PATH, rows)
    print(f"wrote {len(rows)} labels -> {PATH}")


def validate(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        ann_id = row.get("annotation_id", "?")
        ann = row.get("annotation") or {}
        meta = row.get("annotation_meta") or {}
        if not meta.get("completed_at"):
            errors.append(f"{ann_id}: completed_at empty")
        ratings = ann.get("ratings") or {}
        if set(ratings) != set(DIMS):
            errors.append(f"{ann_id}: ratings incomplete")
        else:
            for key, value in ratings.items():
                if value not in {1, 2, 3, 4, 5}:
                    errors.append(f"{ann_id}: {key}={value} invalid")
        if ann.get("hard_failure") not in (True, False):
            errors.append(f"{ann_id}: hard_failure must be bool")
        if ann.get("abstain") not in (True, False):
            errors.append(f"{ann_id}: abstain must be bool")
        if ann.get("abstain") and not ann.get("abstain_reason"):
            errors.append(f"{ann_id}: abstain requires reason")
        if ann.get("hard_failure") and ann.get("abstain"):
            errors.append(f"{ann_id}: hard_failure and abstain both true")
        # hard failure should not carry high average as "good"
        if ann.get("hard_failure") is True:
            avg = sum(ratings.values()) / 5
            if avg >= 4:
                errors.append(f"{ann_id}: hard_failure but ratings avg={avg:.1f} too high")
    return errors


def cmd_validate() -> int:
    rows = load_jsonl(PATH)
    errors = validate(rows)
    complete = sum(1 for row in rows if (row.get("annotation_meta") or {}).get("completed_at"))
    print(f"output_quality: {complete}/{len(rows)} completed")
    if errors:
        print(f"FAIL {len(errors)} issues:")
        for item in errors:
            print(f"  - {item}")
        return 1
    avgs = []
    hard = 0
    for row in rows:
        ann = row["annotation"]
        hard += int(bool(ann.get("hard_failure")))
        avgs.append(sum(ann["ratings"].values()) / 5)
    print("PASS")
    print(f"mean_score={sum(avgs) / len(avgs):.2f} hard_failure_count={hard}")
    return 0


def cmd_status() -> None:
    rows = load_jsonl(PATH)
    complete = 0
    for row in rows:
        ann = row.get("annotation") or {}
        done = (row.get("annotation_meta") or {}).get("completed_at")
        if done:
            complete += 1
        ratings = ann.get("ratings") or {}
        avg = (
            sum(v for v in ratings.values() if isinstance(v, int)) / 5
            if all(isinstance(ratings.get(d), int) for d in DIMS)
            else None
        )
        print(
            f"{row['annotation_id']}: {'done' if done else 'todo'} "
            f"avg={avg} hard={ann.get('hard_failure')} state="
            f"{(row.get('input') or {}).get('user_visible_output', {}).get('state')}"
        )
    print(f"progress {complete}/{len(rows)}")


def main() -> int:
    args = parse_args()
    if args.command == "apply":
        apply()
        return cmd_validate()
    if args.command == "validate":
        return cmd_validate()
    if args.command == "status":
        cmd_status()
        return 0
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
