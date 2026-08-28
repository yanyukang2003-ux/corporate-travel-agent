# 多轮对话 + 多段行程 · 语义入口 · 真实模型

- started_at: `2026-08-28T03:38:10.862353+00:00`
- runner: `semantic-multiturn-model-runner-v1`
- model: `deepseek-v4-pro` · prompt `semantic-trip-intent-v10`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **5/5 PASS**（每条跑 2 次，全部通过才记 PASS）
- 模型调用: 28 次，估算 **0.043504 USD**

MT-03 / MT-04 是安全用例：这两种行程系统**表达不了**，必须一次库存都不查，
尤其不能把用户没要过的返程航线拼出来。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| MT-01 | PASS | 2/2 | 五轮逐步搭出一趟往返：每一轮的信息都不许丢 |
| MT-02 | PASS | 2/2 | 搭完之后改目的地：旧目的地不许残留 |
| MT-03 | PASS | 2/2 | 多城行程：北京→上海→杭州→北京，绝不许压缩成一段 |
| MT-04 | PASS | 2/2 | 开口程：去上海、从杭州回，绝不许悄悄变成上海→北京 |
| MT-05 | PASS | 2/2 | 第四轮推翻第一轮：后说的必须盖住先说的 |

## 每条用例的最终读数

| ID | 状态 | 搜库存 | 航段 |
|---|---|---|---|
| MT-01 | NO_FEASIBLE_OPTION | 2 | [('Beijing', 'Shanghai'), ('Shanghai', 'Beijing')] |
| MT-02 | NO_FEASIBLE_OPTION | 1 | [('Beijing', '广州')] |
| MT-03 | NEEDS_STRUCTURED_INPUT | 0 | [] |
| MT-04 | NEEDS_CLARIFICATION | 0 | [] |
| MT-05 | NEEDS_CLARIFICATION | 3 | [('Beijing', 'Shanghai')] |

