#!/usr/bin/env python3
"""三轮重跑的一致性：比决策，不比文本（离线，$0）。判据在
`evaluation/consistency.py`。

```bash
.venv/bin/python examples/run_consistency_evaluation.py \
  --runs reports/evaluation-runs/external-longtail-multiturn-live-r1-20260902 \
         reports/evaluation-runs/external-longtail-multiturn-live-r2-20260902 \
         reports/evaluation-runs/external-longtail-multiturn-live-r3-20260902 \
  --output reports/evaluation-runs/consistency-NEWDIR [--label 说明]
```

读每轮目录里的 `report.json`（没有搜索参数时退回 `traces.jsonl`），写 `consistency.json`
和 `REPORT.md`。输出目录必须事先不存在。不调模型、不访问外网。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corporate_travel_agent.evaluation.consistency import evaluate_consistency, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", required=True, type=Path, help="至少两轮报告目录")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", default=None, help="写进报告标题的一句说明")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")
    for run in args.runs:
        if not (run / "report.json").exists():
            parser.error(f"没有 report.json：{run}")
    summary = evaluate_consistency(args.runs, label=args.label)
    args.output.mkdir(parents=True)
    (args.output / "consistency.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (args.output / "REPORT.md").write_text(render_markdown(summary), encoding="utf-8")
    l1 = summary["layer_1_decision"]
    l2 = summary["layer_2_search"]
    l4 = summary["layer_4_clarification"]
    print(f"第 1 层 终局决策一致 {l1['consistent']}/{l1['cases']}")
    if l2["status"] == "computed":
        print(
            f"第 2 层 搜索参数去重后一致 {l2['identical_after_dedup']}/"
            f"{l2['searched_in_all_compared_runs']}（去重前 {l2['identical_before_dedup']}；"
            f"比较 {len(l2['compared_runs'])} 轮，跳过 {l2['skipped_runs']}）"
        )
    else:
        print(f"第 2 层 不适用：{l2['reason']}")
    if l4["status"] == "computed":
        print(
            f"第 4 层 追问目标核心事实一致 {l4['identical_core_fact_sets']}/"
            f"{l4['asked_in_all_compared_runs']}（全集一致 {l4['identical_fact_sets']}）"
        )
    else:
        print(f"第 4 层 不适用：{l4['reason']}")
    print(f"报告写入 {args.output / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
