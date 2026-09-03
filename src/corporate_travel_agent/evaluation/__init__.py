"""evaluation：离线评测与真实链路评测的执行器、数据集加载、裁判、轨迹与报告。

2026-09-03 从 `services/evaluation_*.py` 搬到这里（ADR-0008）。它是依赖图的最上层之一：可以用
`agent` / `api` 以下的一切，但**产品代码不许 import 它**——`tests/test_architecture_layers.py`
守着这条边。产品接口需要的指标计算住在 `services/business_metrics.py` 和 `services/metrics.py`。
"""
