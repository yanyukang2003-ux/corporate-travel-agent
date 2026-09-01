# 多轮对话 + 多段行程 · 语义入口 · 真实模型

- started_at: `2026-08-29T20:13:56.409120+00:00`
- runner: `semantic-multiturn-model-runner-v1`
- model: `deepseek-v4-pro` · prompt `semantic-trip-intent-v13d`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **5/5 PASS**（每条跑 2 次，全部通过才记 PASS）
- 模型调用: 31 次，估算 **0.066213 USD**

MT-03 多城、MT-04 开口程：这两种行程**现在都表达得了**（§29 开口程、
§36 多城）。它们盯的不再是「拦没拦住」，而是**有没有被悄悄压缩**——
把三段读成一段、或把返程起点换成目的地，然后拿用户没要过的航线去搜库存。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| MT-01 | PASS | 2/2 | 五轮逐步搭出一趟往返：每一轮的信息都不许丢 |
| MT-02 | PASS | 2/2 | 搭完之后改目的地：旧目的地不许残留 |
| MT-03 | PASS | 2/2 | 多城行程：北京→上海→杭州→北京，三段都要读出来 |
| MT-04 | PASS | 2/2 | 开口程：去上海、从杭州回，按用户说的两段走 |
| MT-05 | PASS | 2/2 | 第四轮推翻第一轮：后说的必须盖住先说的 |

## 每条用例的最终读数

| ID | 状态 | 搜库存 | 航段 |
|---|---|---|---|
| MT-01 | NO_FEASIBLE_OPTION | 3 | [('Beijing', 'Shanghai'), ('Shanghai', 'Beijing')] |
| MT-02 | NO_FEASIBLE_OPTION | 2 | [('Beijing', 'Guangzhou')] |
| MT-03 | NO_FEASIBLE_OPTION | 4 | [('Beijing', 'Shanghai'), ('Shanghai', 'Hangzhou'), ('Hangzhou', 'Beijing')] |
| MT-04 | NO_FEASIBLE_OPTION | 2 | [('Beijing', 'Shanghai'), ('Hangzhou', 'Beijing')] |
| MT-05 | NEEDS_CLARIFICATION | 3 | [('Beijing', 'Shanghai')] |

