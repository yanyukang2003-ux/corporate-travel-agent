#!/usr/bin/env python3
"""从多轮续问真跑的 ``judge-inputs.jsonl`` 抽人工校准样本，出盲标包。

Judge 要能引用，先得有人工双评：协议要求两名标注者、一致率 0.9。这里按终态分层
（办成 / 追问 / 越界 / 诚实失败 / 降级），用哈希顺序确定性抽 N 条，给每位标注者出一份
同样内容、评分留空的文件。记录格式与 ``run_output_quality_judge.py --annotations``
读取的一致（按 ``input.run_id`` 和 Judge 判定对齐）。

```bash
.venv/bin/python examples/build_multiturn_annotation_packet.py \\
  --run-directory reports/evaluation-runs/external-longtail-multiturn-live-r1-20260902 \\
  --output data/evaluation/human-annotations/external-longtail-multiturn-v1 \\
  --sample 100 --annotators human-01,human-02
```

标注完成后把各标注者的文件拼成一个 jsonl 交给 Judge runner 的 ``--annotations``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DIMENSIONS = (
    "completeness",
    "actionability",
    "policy_transparency",
    "evidence_grounding",
    "uncertainty_and_failure_honesty",
)

README = """# 多轮续问 · 输出质量人工标注

每条记录是系统对一位真实用户原话（可能加上模拟续问）的最终可见回复。请只看
`input.user_visible_output`：`traveler_messages` 是旅行者说过的话，`assistant_reply`
是系统回复，`options` 是它摆出的方案（`facts` 是它能引用的事实），`state` 是终态。

五个维度各打 1–5 分（`annotation.ratings`），标准见 `evals/rubrics/output-quality-v1.json`：

- completeness：结果、关键约束、政策状态、缺什么信息，说全了吗
- actionability：下一步该做什么清楚吗，和当前状态匹配吗
- policy_transparency：合规 / 审批 / 拦截状态说得准吗
- evidence_grounding：库存和供应商相关的说法有 `facts` / `evidence_refs` 撑着吗
- uncertainty_and_failure_honesty：失败、不确定、办不到，如实说了吗，有没有编

其他字段：`hard_failure`（有编造、声称已订等硬错就 true）、`abstain`（判不了就 true 并写
`abstain_reason`）、`notes`（一句话理由）。`annotation_meta.completed_at` 填完成时间。
两位标注者独立打分，不要商量。不要修改 `input`。
"""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def stratified_sample(
    inputs: list[dict[str, Any]], *, sample: int, salt: str
) -> list[dict[str, Any]]:
    """按终态分层，每层按哈希顺序取，层间轮转直到取满。确定性，可复现。"""
    by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inputs:
        by_state[item["user_visible_output"]["state"]].append(item)
    for bucket in by_state.values():
        bucket.sort(
            key=lambda item: hashlib.sha256(f"{salt}:{item['case_id']}".encode()).hexdigest()
        )
    picked: list[dict[str, Any]] = []
    states = sorted(by_state)
    cursors = dict.fromkeys(states, 0)
    while len(picked) < min(sample, len(inputs)):
        progressed = False
        for state in states:
            bucket = by_state[state]
            if cursors[state] < len(bucket) and len(picked) < sample:
                picked.append(bucket[cursors[state]])
                cursors[state] += 1
                progressed = True
        if not progressed:
            break
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=100)
    parser.add_argument("--annotators", default="human-01,human-02")
    parser.add_argument("--round", type=int, default=1)
    args = parser.parse_args()

    judge_path = args.run_directory / "judge-inputs.jsonl"
    if not judge_path.is_file():
        parser.error(f"没有 judge-inputs.jsonl：{judge_path}")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(f"输出目录已存在且非空：{args.output}")
    annotators = [item.strip() for item in args.annotators.split(",") if item.strip()]
    if len(annotators) < 2:
        parser.error("至少两名标注者，否则校准状态只能是 insufficient_samples")

    inputs = load_jsonl(judge_path)
    picked = stratified_sample(inputs, sample=args.sample, salt=args.run_directory.name)
    source_dataset = args.run_directory.name
    ordered = sorted(
        picked,
        key=lambda item: hashlib.sha256(
            f"annotation:{source_dataset}:{item['case_id']}".encode()
        ).hexdigest(),
    )
    mappings = []
    per_annotator: dict[str, list[dict[str, Any]]] = {name: [] for name in annotators}
    for index, item in enumerate(ordered, start=1):
        annotation_id = f"MT-{index:03d}"
        mappings.append(
            {
                "annotation_id": annotation_id,
                "source_case_id": item["case_id"],
                "run_id": item["run_id"],
                "state": item["user_visible_output"]["state"],
            }
        )
        for name in annotators:
            per_annotator[name].append(
                {
                    "schema_version": 1,
                    "annotation_id": annotation_id,
                    "annotation_task": "output_quality",
                    "annotation_meta": {
                        "annotator_id": name,
                        "round": args.round,
                        "blinded": True,
                        "completed_at": None,
                    },
                    "source_dataset": source_dataset,
                    "input": {
                        "run_id": item["run_id"],
                        "scenario": item["scenario"],
                        "expected_constraints": item["expected_constraints"],
                        "trace_evidence_refs": item["trace_evidence_refs"],
                        "user_visible_output": item["user_visible_output"],
                        "candidate_identity_blinded": True,
                    },
                    "annotation": {
                        "ratings": dict.fromkeys(DIMENSIONS),
                        "hard_failure": None,
                        "abstain": None,
                        "abstain_reason": None,
                        "evidence": {},
                        "notes": None,
                    },
                }
            )
    files = {
        f"to-label/output-quality-{name}.jsonl": rows for name, rows in per_annotator.items()
    }
    files["private/source-id-map.jsonl"] = mappings
    for relative, rows in files.items():
        write_jsonl(args.output / relative, rows)
    (args.output / "README.md").write_text(README, encoding="utf-8")
    states = defaultdict(int)
    for item in mappings:
        states[item["state"]] += 1
    manifest = {
        "schema_version": 1,
        "packet_id": f"{source_dataset}-annotations",
        "status": "unlabeled",
        "created_at": datetime.now(UTC).isoformat(),
        "source_run_directory": str(args.run_directory),
        "judge_inputs_sha256": hashlib.sha256(judge_path.read_bytes()).hexdigest(),
        "available": len(inputs),
        "sampled": len(mappings),
        "by_state": dict(states),
        "annotators": annotators,
        "round": args.round,
        "files": {
            relative: hashlib.sha256((args.output / relative).read_bytes()).hexdigest()
            for relative in files
        },
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"抽样 {len(mappings)}/{len(inputs)}，按终态 {dict(states)}，"
        f"{len(annotators)} 份待标注文件写到 {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
