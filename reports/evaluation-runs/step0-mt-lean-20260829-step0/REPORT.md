# 多轮对话 + 多段行程 · 语义入口 · 真实模型

- started_at: `2026-08-29T20:16:59.539952+00:00`
- runner: `semantic-multiturn-model-runner-v1`
- model: `deepseek-v4-pro` · prompt `semantic-trip-intent-lean-v1`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **3/5 PASS**（每条跑 2 次，全部通过才记 PASS）
- 模型调用: 27 次，估算 **0.049868 USD**

MT-03 多城、MT-04 开口程：这两种行程**现在都表达得了**（§29 开口程、
§36 多城）。它们盯的不再是「拦没拦住」，而是**有没有被悄悄压缩**——
把三段读成一段、或把返程起点换成目的地，然后拿用户没要过的航线去搜库存。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| MT-01 | PASS | 2/2 | 五轮逐步搭出一趟往返：每一轮的信息都不许丢 |
| MT-02 | PASS | 2/2 | 搭完之后改目的地：旧目的地不许残留 |
| MT-03 | FAIL | 0/2 | 多城行程：北京→上海→杭州→北京，三段都要读出来 |
| MT-04 | FAIL | 0/2 | 开口程：去上海、从杭州回，按用户说的两段走 |
| MT-05 | PASS | 2/2 | 第四轮推翻第一轮：后说的必须盖住先说的 |

## 没过的用例

### MT-03 — 多城行程：北京→上海→杭州→北京，三段都要读出来

- 轮0：`9月15号从北京去上海开会，然后去杭州见客户，最后回北京` → NEEDS_CLARIFICATION
- 轮1：`上海会议9月16号上午10点，杭州9月18号下午2点见客户，9月19号晚上回北京` → NEEDS_CLARIFICATION
- 轮2：`9月19号晚上11点前到北京就行` → NEEDS_CLARIFICATION
- 最终状态：`NEEDS_CLARIFICATION` · 航段 `[]`
- 追问：请问您从上海到杭州的具体出发时间是什么？另外，是否需要安排住宿？
- **legs**：[]

### MT-04 — 开口程：去上海、从杭州回，按用户说的两段走

- 轮0：`去程9月15号北京飞上海，返程9月20号从杭州飞回北京` → NEEDS_CLARIFICATION
- 轮1：`9月16号中午12点前到上海就行，返程9月20号晚上8点前到北京` → NEEDS_CLARIFICATION
- 最终状态：`NEEDS_CLARIFICATION` · 航段 `[]`
- 追问：另外我还需要知道：哪天返程。
- **legs**：[]

## 每条用例的最终读数

| ID | 状态 | 搜库存 | 航段 |
|---|---|---|---|
| MT-01 | NO_FEASIBLE_OPTION | 2 | [('Beijing', 'Shanghai'), ('Shanghai', 'Beijing')] |
| MT-02 | NO_FEASIBLE_OPTION | 2 | [('Beijing', 'Guangzhou')] |
| MT-03 | NEEDS_CLARIFICATION | 0 | [] |
| MT-04 | NEEDS_CLARIFICATION | 0 | [] |
| MT-05 | NEEDS_CLARIFICATION | 3 | [('Beijing', 'Shanghai')] |

