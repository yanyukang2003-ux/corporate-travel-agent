# 多轮对话 + 多段行程 · 语义入口 · 真实模型

- started_at: `2026-08-28T03:27:23.614460+00:00`
- runner: `semantic-multiturn-model-runner-v1`
- model: `deepseek-v4-pro` · prompt `semantic-trip-intent-v9`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **3/5 PASS**（每条跑 1 次，全部通过才记 PASS）
- 模型调用: 14 次，估算 **0.021744 USD**

MT-03 / MT-04 是安全用例：这两种行程系统**表达不了**，必须一次库存都不查，
尤其不能把用户没要过的返程航线拼出来。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| MT-01 | FAIL | 0/1 | 五轮逐步搭出一趟往返：每一轮的信息都不许丢 |
| MT-02 | FAIL | 0/1 | 搭完之后改目的地：旧目的地不许残留 |
| MT-03 | PASS | 1/1 | 多城行程：北京→上海→杭州→北京，绝不许压缩成一段 |
| MT-04 | PASS | 1/1 | 开口程：去上海、从杭州回，绝不许悄悄变成上海→北京 |
| MT-05 | PASS | 1/1 | 第四轮推翻第一轮：后说的必须盖住先说的 |

## 没过的用例

### MT-01 — 五轮逐步搭出一趟往返：每一轮的信息都不许丢

- 轮0：`下个月要去上海出差` → NEEDS_CLARIFICATION
- 轮1：`从北京出发` → NEEDS_CLARIFICATION
- 轮2：`9月15号走` → NEEDS_CLARIFICATION
- 轮3：`9月16号中午12点前必须到` → NEEDS_CLARIFICATION
- 轮4：`9月18号晚上6点以后到11点前起飞回北京` → NEEDS_CLARIFICATION
- 最终状态：`NEEDS_CLARIFICATION` · 航段 `[]`
- 追问：请确认这些出行信息后我再搜索：origin、destination。
- **legs**：[]

### MT-02 — 搭完之后改目的地：旧目的地不许残留

- 轮0：`9月15号从北京去上海，9月16号中午12点前到` → NEEDS_CLARIFICATION
- 轮1：`改成去广州，其他不变` → NO_FEASIBLE_OPTION
- 最终状态：`NO_FEASIBLE_OPTION` · 航段 `[('Beijing', '广州')]`
- **destination**：expected Guangzhou, got 广州

## 每条用例的最终读数

| ID | 状态 | 搜库存 | 航段 |
|---|---|---|---|
| MT-01 | NEEDS_CLARIFICATION | 0 | [] |
| MT-02 | NO_FEASIBLE_OPTION | 1 | [('Beijing', '广州')] |
| MT-03 | NEEDS_STRUCTURED_INPUT | 0 | [] |
| MT-04 | NEEDS_CLARIFICATION | 0 | [] |
| MT-05 | NEEDS_CLARIFICATION | 3 | [('Beijing', 'Shanghai')] |

