# 业务结果指标

协议：`business-outcome-v1`　生成时间：2026-09-01T15:14:04.961989+00:00　任务数：5

**这一层量的不是模型答得好不好，是这东西对企业有没有用。**
分母为零的指标写「测不出来」，不写 0——一条平的曲线和一条没有数据的曲线
是两回事。

| 指标 | 这个数是什么 | 值 | 分子/分母 |
|---|---|---|---|
| `handoff_completion_rate` | 系统给出了方案的任务里，员工说「已去官方平台订好」的比例 | 40.0% | 2 / 5 |
| `booking_confirmation_rate` | 系统给出了方案的任务里，员工回填了订单号和实付金额的比例（自述） | 20.0% | 1 / 5 |
| `booking_confirmation_rate.of_handed_off` | 说了「已订好」的任务里，回来填了订单号的比例 | 50.0% | 1 / 2 |
| `booked_cost_variance_ratio_mean` | 实付比方案价多付或少付了几成，平均（只算币种一致的） | +0.9% | 0.00851064 / 1 |
| `expense_reconciled_rate` | 回填了订单号的任务里，费控对上账的比例（对上了才算核实过） | 100.0% | 1 / 1 |
| `reconciliation_variance_ratio_mean` | 费控金额比员工自述多或少了几成，平均 | +0.6% | 0.00632911 / 1 |
| `change_auto_planned_rate` | 改期任务里系统直接摆出方案、没让人插手的比例 | 100.0% | 1 / 1 |
| `change_intervention_rate` | 改期任务里人不得不插手的比例（变更场景人工介入率） | 0.0% | 0 / 1 |
| `off_channel_expense_rate` | 费控记录里在本系统找不到对应确认的比例（绕开系统订的） | 50.0% | 1 / 2 |
| `handoff_completion_rate.all_tasks` | 全部任务里说「已订好」的比例（含系统没能给出方案的） | 40.0% | 2 / 5 |
| `seconds_to_handoff_mean` | 从任务开始到说「已订好」的平均墙上时间（秒） | 0.000989 seconds | 0.001978 / 2 |
| `seconds_to_handoff_p50` | 同上，中位数（秒） | 0.000989 seconds | — / 2 |
| `seconds_to_handoff_p95` | 同上，95 分位（秒） | 0.001046 seconds | — / 2 |
| `advance_booking_days_mean` | 订好那一刻距离出发还有几天，平均 | 3.889 days | 7.77778 / 2 |
| `advance_booking_days_p50` | 订好那一刻距离出发还有几天，中位数 | 3.889 days | — / 2 |
| `policy_violation_rate.selected` | 员工选中的方案里，需要审批或被禁的比例 | 33.3% | 1 / 3 |
| `policy_violation_rate.offered` | 展示出去的方案里，需要审批或被禁的比例 | 71.4% | 10 / 14 |
| `approval_request_rate` | 走了例外审批的任务比例 | 20.0% | 1 / 5 |
| `clarification_rounds_mean` | 平均追问了几轮 | 0 rounds | 0 / 5 |
| `tool_calls_per_task_mean` | 平均用掉几次工具调用（含模型和供应商） | 3.8 calls | 19 / 5 |

## 口径说明

- **`handoff_completion_rate`**：分子是员工在本系统里点的「已在官方平台订好」，不是供应商回执；这是渠道内预订率的上限估计，不是渠道内预订率本身。分母只算系统真的给出过方案的任务。要看更硬一点的数，读 booking_confirmation_rate。
- **`booking_confirmation_rate`**：分子是员工回填了订单号和实付金额的任务。仍是自述不是回执——订单号系统核不了；比「点了一下」多的是一个具体的号和一个具体的数。分母只算系统真的给出过方案的任务。
- **`booking_confirmation_rate.of_handed_off`**：说了「我去订了」的人里，回来填了订单号的比例。1 减它就是交接完成率高估了多少。
- **`booked_cost_variance_ratio_mean`**：（实付 − 方案价）÷ 方案价，正数是多付。只算币种一致的确认记录；币种不一致的任务带 confirmation_currency_mismatch 说明，不换算。
- **`expense_reconciled_rate`**：回填了订单号的任务里，费控记录对上了的比例。对上了的自述才算「核实过」；费控没接时分母有、分子为 0，这个数就是 0——那是事实，不是测不出来。
- **`reconciliation_variance_ratio_mean`**：（费控金额 − 自述金额）÷ 自述金额，只算币种一致的对账记录。正数是员工少报、负数是多报。
- **`change_auto_planned_rate`**：航变/会议改期开出来的改期任务里，系统不经人插手就摆出了方案的比例。没有改期任务时测不出来。
- **`change_intervention_rate`**：改期任务里人不得不插手（改需求、补表、没方案、供应商失败）的比例——设计文档里的「变更场景人工介入率」。
- **`off_channel_expense_rate`**：导入的费控记录里，本系统找不到对应下单确认的比例——员工绕开系统订的那部分。这是设计文档里「渠道内预订率」的补集，第一次有了真值；费控没接时测不出来。
- **`handoff_completion_rate.all_tasks`**：分母是全部任务，含系统没能给出方案的那些。和上一条的差值是「系统没干成活」而不是「员工不愿用」。
- **`seconds_to_handoff_mean`**：墙上时间，含员工离开去开会、经理隔天才批的等待。系统自身耗时见 evaluation_performance。
- **`seconds_to_handoff_p50`**：线性 type-7 分位数，口径同 mean。
- **`seconds_to_handoff_p95`**：线性 type-7 分位数，口径同 mean。
- **`advance_booking_days_mean`**：起点优先用员工回填的下单时刻（booking_confirmation）；没回填的任务用交接完成时刻。终点是首段实际出发时刻。起点不用任务创建时刻，因为下单不发生在那一刻。
- **`advance_booking_days_p50`**：线性 type-7 分位数，口径同 mean。
- **`policy_violation_rate.selected`**：员工最后选中的方案里带「需审批/禁止」证据的比例。「判不了」（证据不足）不计入——那是缺数据，不是超标。
- **`policy_violation_rate.offered`**：展示给员工的方案里带「需审批/禁止」证据的比例。按已编码不变量，FORBIDDEN 方案在规划阶段已被硬过滤，所以这个数实际上量的是「需要审批」的比例。
- **`approval_request_rate`**：走了例外审批的任务比例。
- **`clarification_rounds_mean`**：每个任务追问了几轮；口径为全部任务。
- **`tool_calls_per_task_mean`**：计入预算的工具调用次数；口径为全部任务。
