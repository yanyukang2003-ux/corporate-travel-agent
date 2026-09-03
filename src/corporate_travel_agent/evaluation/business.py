"""业务结果指标的评测侧：把 `services/business_metrics.py` 算出来的报告写进一个新的报告目录。

计算本身是产品代码（接口和看板也读它），这里只负责落盘和渲染成人读的 Markdown。
口径、时钟和分母为零的规矩见 `services/business_metrics.py` 模块开头。
"""

from __future__ import annotations

from pathlib import Path

from corporate_travel_agent.services.business_metrics import (
    BOOKING_CONFIRMED_EVENT_TYPE,
    BUSINESS_PROTOCOL_ID,
    CHANGE_INTERVENTION_EVENT_TYPES,
    HANDED_OFF_STATES,
    HANDOFF_COMPLETED_EVENT_TYPE,
    METRIC_LABELS,
    TASK_START_EVENT_TYPES,
    VIOLATION_OUTCOMES,
    BusinessMetricsError,
    BusinessMetricsReport,
    BusinessModel,
    TaskBusinessRecord,
    aggregate_metrics,
    build_business_metrics_report,
    summarize_task,
)

__all__ = [
    "BOOKING_CONFIRMED_EVENT_TYPE",
    "BUSINESS_PROTOCOL_ID",
    "CHANGE_INTERVENTION_EVENT_TYPES",
    "HANDED_OFF_STATES",
    "HANDOFF_COMPLETED_EVENT_TYPE",
    "METRIC_LABELS",
    "TASK_START_EVENT_TYPES",
    "VIOLATION_OUTCOMES",
    "BusinessMetricsError",
    "BusinessMetricsReport",
    "BusinessModel",
    "TaskBusinessRecord",
    "aggregate_metrics",
    "build_business_metrics_report",
    "summarize_task",
    "write_business_metrics_report",
    "render_business_report_markdown",
]


def write_business_metrics_report(
    report: BusinessMetricsReport,
    output_directory: str | Path,
) -> Path:
    """把报告写进一个**事先不存在**的目录，返回该目录。

    沿用仓库既有约定：输出目录必须是新的。这不是洁癖——评测目录是证据，
    覆盖一次就没有第二份可比。

    产出两个文件：机器读的 `report.json` 和人读的 `REPORT.md`。
    """
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise BusinessMetricsError(f"Output directory already exists: {output_root}")
    output_root.mkdir(parents=True)
    (output_root / "report.json").write_bytes(
        report.model_dump_json(indent=2).encode("utf-8")
    )
    (output_root / "REPORT.md").write_bytes(render_business_report_markdown(report).encode("utf-8"))
    return output_root


def render_business_report_markdown(report: BusinessMetricsReport) -> str:
    """把报告渲染成给人读的 Markdown：每个数字都带着它的口径一起出现。"""
    lines = [
        "# 业务结果指标",
        "",
        f"协议：`{report.protocol_id}`　生成时间：{report.generated_at.isoformat()}　"
        f"任务数：{report.task_count}",
        "",
        "**这一层量的不是模型答得好不好，是这东西对企业有没有用。**",
        "分母为零的指标写「测不出来」，不写 0——一条平的曲线和一条没有数据的曲线",
        "是两回事。",
        "",
        "| 指标 | 这个数是什么 | 值 | 分子/分母 |",
        "|---|---|---|---|",
    ]
    for name, metric in report.metrics.items():
        label = METRIC_LABELS.get(name, "（这个指标还没写口径说明）")
        if metric.status != "measured" or metric.value is None:
            value = "测不出来"
        elif metric.unit == "rate":
            value = f"{metric.value:.1%}"
        elif metric.unit == "ratio":
            value = f"{metric.value:+.1%}"
        else:
            # 用有效数字而不是固定两位小数：秒级统计在演示里可能是 0.001，
            # 写成 "0.00 秒" 会让人以为没测到。
            value = f"{metric.value:.4g} {metric.unit or ''}".strip()
        numerator = "—" if metric.numerator is None else f"{metric.numerator:g}"
        denominator = "—" if metric.denominator is None else f"{metric.denominator:g}"
        lines.append(f"| `{name}` | {label} | {value} | {numerator} / {denominator} |")

    notes = sorted({note for item in report.records for note in item.notes})
    lines.extend(["", "## 口径说明", ""])
    for name, metric in report.metrics.items():
        if metric.confidence_note:
            lines.append(f"- **`{name}`**：{metric.confidence_note}")

    if notes:
        lines.extend(["", "## 这批任务上出现过的测不准原因", ""])
        for note in notes:
            count = sum(1 for item in report.records if note in item.notes)
            lines.append(f"- `{note}`：{count} 个任务")
    return "\n".join(lines) + "\n"
