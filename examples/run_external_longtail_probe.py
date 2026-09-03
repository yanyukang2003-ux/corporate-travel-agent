#!/usr/bin/env python3
"""外部真话长尾探针（离线替身，$0）。判据在
`services/evaluation_external_longtail.py`，与真模型抽样 runner 共用同一份。

```bash
.venv/bin/python examples/run_external_longtail_probe.py \
  --dataset data/evaluation/external-longtail-v1 \
  --output reports/evaluation-runs/external-longtail-probe-NEWDIR [--gate] [--limit N]
```

输出目录必须事先不存在。不调计费模型、不访问外网。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corporate_travel_agent.evaluation.external_longtail import (
    load_cases,
    probe_cases,
    render_markdown,
    summarize,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("data/evaluation/external-longtail-v1")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")
    cases, manifest = load_cases(args.dataset)
    if args.limit:
        cases = cases[: args.limit]
    results = probe_cases(cases)
    summary = summarize(results, manifest)
    args.output.mkdir(parents=True)
    (args.output / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (args.output / "REPORT.md").write_text(render_markdown(summary), encoding="utf-8")
    print(
        f"红线 {summary['passed']}/{summary['total']}；崩溃 {summary['crashed']}；"
        f"终态 {json.dumps(summary['state_distribution'], ensure_ascii=False)}"
    )
    print(f"报告写入 {args.output / 'REPORT.md'}")
    return 1 if args.gate and summary["passed"] != summary["total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
