# 业务结果指标

> **这是演示数据，不是真实业务数字。** 由 `examples/measure_business_outcomes.py --demo`
> 生成：跑三条演示流程（订到底 / 看了没订 / 走例外审批）各一条，让每个分母都非零，
> 用来确认报告格式和口径说明是对的。**任何一个数都不能拿去下结论。**
> 真实数字要从 PostgreSQL 里的真实任务算：加 `--database-url`。

协议：`business-outcome-v1`　生成时间：2026-09-01T03:18:00.302126+00:00　任务数：3

**这一层量的不是模型答得好不好，是这东西对企业有没有用。**
分母为零的指标写「测不出来」，不写 0——一条平的曲线和一条没有数据的曲线
是两回事。

| 指标 | 这个数是什么 | 值 | 分子/分母 |
|---|---|---|---|
| `handoff_completion_rate` | 系统给出了方案的任务里，员工说「已去官方平台订好」的比例 | 33.3% | 1 / 3 |
| `handoff_completion_rate.all_tasks` | 全部任务里说「已订好」的比例（含系统没能给出方案的） | 33.3% | 1 / 3 |
| `seconds_to_handoff_mean` | 从任务开始到说「已订好」的平均墙上时间（秒） | 0.000996 seconds | 0.000996 / 1 |
| `seconds_to_handoff_p50` | 同上，中位数（秒） | 0.000996 seconds | — / 1 |
| `seconds_to_handoff_p95` | 同上，95 分位（秒） | 0.000996 seconds | — / 1 |
| `advance_booking_days_mean` | 订好那一刻距离出发还有几天，平均 | 3.889 days | 3.88889 / 1 |
| `advance_booking_days_p50` | 订好那一刻距离出发还有几天，中位数 | 3.889 days | — / 1 |
| `policy_violation_rate.selected` | 员工选中的方案里，需要审批或被禁的比例 | 50.0% | 1 / 2 |
| `policy_violation_rate.offered` | 展示出去的方案里，需要审批或被禁的比例 | 66.7% | 6 / 9 |
| `approval_request_rate` | 走了例外审批的任务比例 | 33.3% | 1 / 3 |
| `clarification_rounds_mean` | 平均追问了几轮 | 0 rounds | 0 / 3 |
| `tool_calls_per_task_mean` | 平均用掉几次工具调用（含模型和供应商） | 3.667 calls | 11 / 3 |

## 口径说明

- **`handoff_completion_rate`**：分子是员工在本系统里点的「已在官方平台订好」，不是供应商回执；这是渠道内预订率的上限估计，不是渠道内预订率本身。分母只算系统真的给出过方案的任务。
- **`handoff_completion_rate.all_tasks`**：分母是全部任务，含系统没能给出方案的那些。和上一条的差值是「系统没干成活」而不是「员工不愿用」。
- **`seconds_to_handoff_mean`**：墙上时间，含员工离开去开会、经理隔天才批的等待。系统自身耗时见 evaluation_performance。
- **`seconds_to_handoff_p50`**：线性 type-7 分位数，口径同 mean。
- **`seconds_to_handoff_p95`**：线性 type-7 分位数，口径同 mean。
- **`advance_booking_days_mean`**：从交接完成时刻算到首段实际出发时刻。起点用交接而不是任务创建，因为下单发生在交接那一刻。
- **`advance_booking_days_p50`**：线性 type-7 分位数，口径同 mean。
- **`policy_violation_rate.selected`**：员工最后选中的方案里带「需审批/禁止」证据的比例。「判不了」（证据不足）不计入——那是缺数据，不是超标。
- **`policy_violation_rate.offered`**：展示给员工的方案里带「需审批/禁止」证据的比例。按已编码不变量，FORBIDDEN 方案在规划阶段已被硬过滤，所以这个数实际上量的是「需要审批」的比例。
- **`approval_request_rate`**：走了例外审批的任务比例。
- **`clarification_rounds_mean`**：每个任务追问了几轮；口径为全部任务。
- **`tool_calls_per_task_mean`**：计入预算的工具调用次数；口径为全部任务。
