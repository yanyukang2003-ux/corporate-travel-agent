"""带状态的指标结果：产品接口和离线评测共用的一种"数"。

`status == "unavailable"` 时 `value` 是 None——分母为零的指标写"测不出来"，不写 0：一条平的曲线
和一条没有数据的曲线是两回事。这个类型原先住在评测包里；业务指标接口（`GET /metrics/business`）
也要用它，所以它是产品代码。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

MetricStatus = Literal["measured", "unavailable", "not_applicable"]


class MetricModel(BaseModel):
    """指标模型基类：不许多字段，类型严格。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class MetricResult(MetricModel):
    """带状态与说明的指标结果。"""

    status: MetricStatus
    value: float | None
    numerator: float | None
    denominator: float | None
    unit: str | None
    exposure_note: str | None = None
    confidence_note: str | None = None
