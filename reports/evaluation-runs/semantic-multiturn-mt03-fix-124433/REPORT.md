# 多轮对话 + 多段行程 · 语义入口 · 真实模型

- started_at: `2026-08-29T16:44:34.098804+00:00`
- runner: `semantic-multiturn-model-runner-v1`
- model: `deepseek-v4-pro` · prompt `semantic-trip-intent-v13d`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **1/1 PASS**（每条跑 2 次，全部通过才记 PASS）
- 模型调用: 6 次，估算 **0.013963 USD**

MT-03 多城、MT-04 开口程：这两种行程**现在都表达得了**（§29 开口程、
§36 多城）。它们盯的不再是「拦没拦住」，而是**有没有被悄悄压缩**——
把三段读成一段、或把返程起点换成目的地，然后拿用户没要过的航线去搜库存。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| MT-03 | PASS | 2/2 | 多城行程：北京→上海→杭州→北京，三段都要读出来 |

## 每条用例的最终读数

| ID | 状态 | 搜库存 | 航段 |
|---|---|---|---|
| MT-03 | NO_FEASIBLE_OPTION | 3 | [('Beijing', 'Shanghai'), ('Shanghai', 'Hangzhou'), ('Hangzhou', 'Beijing')] |

