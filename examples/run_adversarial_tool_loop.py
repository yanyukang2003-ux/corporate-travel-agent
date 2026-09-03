#!/usr/bin/env python3
"""对抗集（工具循环版）runner：0 计费、0 外部调用，几秒跑完。

    .venv/bin/python examples/run_adversarial_tool_loop.py \\
      --output reports/evaluation-runs/adversarial-tool-loop-NEWDIR [--gate]

输出目录必须事先不存在。判据和用例都在 `services/evaluation_adversarial.py`。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from corporate_travel_agent.evaluation.adversarial import (
    render_markdown,
    run_adversarial_suite,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")

    report = run_adversarial_suite()
    args.output.mkdir(parents=True)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (args.output / "REPORT.md").write_text(render_markdown(report), encoding="utf-8")
    for item in report["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        failed = [check["name"] for check in item["checks"] if not check["ok"]]
        suffix = f" failed={failed}" if failed else ""
        print(f"[{mark}] {item['case_id']} {item['title']} state={item['state']}{suffix}")
    print(f"\n{report['passed']}/{report['total']} 条通过")
    print(f"报告写入 {args.output / 'REPORT.md'}")
    return 1 if args.gate and report["passed"] != report["total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
