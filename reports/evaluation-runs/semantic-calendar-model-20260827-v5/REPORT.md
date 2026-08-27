# §18.3 H 日期边角 · 语义入口 · 真实模型

- started_at: `2026-08-27T18:25:19.946071+00:00`
- runner: `semantic-calendar-model-runner-v1`
- model: `deepseek-v4-pro`
- entrypoint: `semantic`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **2/8 PASS**
- 估算费用: **0.003701 USD**（价目表 `model-prices-openai-20260802-v1`，缓存折扣未建模）
- 模型调用: 8 次，其中 5 次返回了不合规 JSON。**失败调用照样计费但拿不到 usage，不在台账里，所以上面的数字偏低。**

**这是基线测量，不是门禁。** 语义链路在这 8 条上此前没有任何数据。
期望值从 `examples/run_calendar_edge_acceptance.py` 原样搬来，不是本轮新写的答案。

| ID | Result | Title |
|---|---|---|
| H-01 | FAIL | 下下周三 = 下下个星期的星期三 |
| H-02 | PASS | 这周五还是下周五：不许自己挑一个 |
| H-03a | FAIL | 8/5 在该日之前 = 今年 8 月 5 日 |
| H-03b | FAIL | 8.5 在该日之前 = 今年 8 月 5 日 |
| H-03c | FAIL | 2026.8.5 写了年份就按年份，哪怕已经过去 |
| H-03d | FAIL | 没写年份的 8/5 落在刚过去的日子：不许自动跳到明年 |
| H-04 | PASS | 春节不许编成某个公历日 |
| H-05 | FAIL | 12月30日去、1月2日回：返程跨到下一年 |

## 没过的用例

### H-01 — 下下周三 = 下下个星期的星期三

- 用户原话：`下下周三从北京去上海开会`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_CLARIFICATION`
- 追问：请问您说的“下下周三”具体是哪一天？
- **departure_date**：expected 2026-09-02, got None

### H-03a — 8/5 在该日之前 = 今年 8 月 5 日

- 用户原话：`8/5从北京去上海开会`
- 参照时刻：`2026-08-01T09:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **departure_date**：expected 2026-08-05, got None
- **interpretation_succeeded**：Ready semantic intent lacks grounded evidence for: arrive_by

### H-03b — 8.5 在该日之前 = 今年 8 月 5 日

- 用户原话：`8.5从北京去上海开会`
- 参照时刻：`2026-08-01T09:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **departure_date**：expected 2026-08-05, got None
- **interpretation_succeeded**：Ready semantic intent lacks grounded evidence for: arrive_by

### H-03c — 2026.8.5 写了年份就按年份，哪怕已经过去

- 用户原话：`2026.8.5从北京去上海开会`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **departure_date**：expected 2026-08-05, got None
- **interpretation_succeeded**：Ready semantic intent lacks grounded evidence for: arrive_by

### H-03d — 没写年份的 8/5 落在刚过去的日子：不许自动跳到明年

- 用户原话：`8/5从北京去上海开会`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **interpretation_succeeded**：Ready semantic intent lacks grounded evidence for: arrive_by

### H-05 — 12月30日去、1月2日回：返程跨到下一年

- 用户原话：`12月30日从北京去上海开会，1月2日回`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **departure_date**：expected 2026-12-30, got None
- **return_date**：expected 2027-01-02, got None
- **interpretation_succeeded**：Ready semantic intent lacks grounded evidence for: arrive_by

## 全部用例的模型读数

| ID | 状态 | departure_after | arrive_by | return_after |
|---|---|---|---|---|
| H-01 | NEEDS_CLARIFICATION | — | — | — |
| H-02 | NEEDS_CLARIFICATION | — | — | — |
| H-03a | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-03b | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-03c | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-03d | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-04 | NEEDS_CLARIFICATION | — | — | — |
| H-05 | NEEDS_STRUCTURED_INPUT | — | — | — |

