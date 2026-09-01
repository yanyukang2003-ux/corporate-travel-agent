# §18.3 H 日期边角 · 语义入口 · 真实模型

- started_at: `2026-08-29T20:12:21.490552+00:00`
- runner: `semantic-calendar-model-runner-v1`
- model: `deepseek-v4-pro`
- entrypoint: `semantic`
- inventory: `mock`（不碰 Duffel / LiteAPI）
- result: **5/8 PASS**（每条跑 2 次，全部通过才记 PASS）
- 估算费用: **0.021834 USD**（价目表 `model-prices-openai-20260802-v1`，缓存折扣未建模）
- 模型调用: 13 次，其中 0 次返回了不合规 JSON。**失败调用照样计费但拿不到 usage，不在台账里，所以上面的数字偏低。**

**这是基线测量，不是门禁。** 语义链路在这 8 条上此前没有任何数据。
期望值从 `examples/run_calendar_edge_acceptance.py` 原样搬来，不是本轮新写的答案。

| ID | Result | 通过率 | Title |
|---|---|---|---|
| H-01 | FAIL | 0/2 | 下下周三 = 下下个星期的星期三 |
| H-02 | FAIL | 1/2 | 这周五还是下周五：不许自己挑一个 |
| H-03a | PASS | 2/2 | 8/5 在该日之前 = 今年 8 月 5 日 |
| H-03b | PASS | 2/2 | 8.5 在该日之前 = 今年 8 月 5 日 |
| H-03c | PASS | 2/2 | 2026.8.5 写了年份就按年份，哪怕已经过去 |
| H-03d | PASS | 2/2 | 没写年份的 8/5 已经过去：问哪一年或报日期已过，绝不顺延到明年 |
| H-04 | FAIL | 1/2 | 春节不许编成某个公历日 |
| H-05 | PASS | 2/2 | 12月30日去、1月2日回：返程跨到下一年 |

**时好时坏（同一句话不同次结果不同）：** H-02、H-04。这类用例的单次结果是噪声，不能当作退步或修复的证据。

## 没过的用例

### H-01 — 下下周三 = 下下个星期的星期三

- 用户原话：`下下周三从北京去上海开会`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **departure_date**：expected 2026-09-02, got None
- **interpretation_succeeded**：Semantic intent JSON failed validation: 1 validation error for IntentDecision
intent.booking_scope
  Input should be 'OUTBOUND_ONLY', 'RETURN_ONLY' or 'ROUND_TRIP' [type=enum, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/enum

### H-02 — 这周五还是下周五：不许自己挑一个

- 用户原话：`这周五还是下周五从北京去上海`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **clarifies**：NEEDS_STRUCTURED_INPUT
- **question_names_the_ambiguity**：
- **interpretation_succeeded**：Semantic intent JSON failed validation: 1 validation error for IntentDecision
intent.booking_scope
  Input should be 'OUTBOUND_ONLY', 'RETURN_ONLY' or 'ROUND_TRIP' [type=enum, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/enum

### H-04 — 春节不许编成某个公历日

- 用户原话：`春节从北京去上海出差`
- 参照时刻：`2026-08-19T15:00:00+08:00`
- 任务状态：`NEEDS_STRUCTURED_INPUT`
- **clarifies**：NEEDS_STRUCTURED_INPUT
- **origin**：None
- **destination**：None
- **interpretation_succeeded**：Semantic intent JSON failed validation: 1 validation error for IntentDecision
intent.booking_scope
  Input should be 'OUTBOUND_ONLY', 'RETURN_ONLY' or 'ROUND_TRIP' [type=enum, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/enum

## 全部用例的模型读数

| ID | 状态 | departure_after | arrive_by | return_after |
|---|---|---|---|---|
| H-01 | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-02 | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-03a | NEEDS_CLARIFICATION | 2026-08-05 00:00:00+08:00 | — | — |
| H-03b | NEEDS_CLARIFICATION | 2026-08-05 00:00:00+08:00 | — | — |
| H-03c | NEEDS_CLARIFICATION | 2026-08-05 00:00:00+08:00 | — | — |
| H-03d | NEEDS_CLARIFICATION | 2026-08-05 00:00:00+08:00 | — | — |
| H-04 | NEEDS_STRUCTURED_INPUT | — | — | — |
| H-05 | NEEDS_CLARIFICATION | 2026-12-30 00:00:00+08:00 | — | 2027-01-02 00:00:00+08:00 |

