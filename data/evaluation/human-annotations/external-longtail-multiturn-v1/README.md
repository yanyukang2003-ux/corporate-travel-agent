# 多轮续问 · 输出质量人工标注

每条记录是系统对一位真实用户原话（可能加上模拟续问）的最终可见回复。请只看
`input.user_visible_output`：`traveler_messages` 是旅行者说过的话，`assistant_reply`
是系统回复，`options` 是它摆出的方案（`facts` 是它能引用的事实），`state` 是终态。

五个维度各打 1–5 分（`annotation.ratings`），标准见 `evals/rubrics/output-quality-v1.json`：

- completeness：结果、关键约束、政策状态、缺什么信息，说全了吗
- actionability：下一步该做什么清楚吗，和当前状态匹配吗
- policy_transparency：合规 / 审批 / 拦截状态说得准吗
- evidence_grounding：库存和供应商相关的说法有 `facts` / `evidence_refs` 撑着吗
- uncertainty_and_failure_honesty：失败、不确定、办不到，如实说了吗，有没有编

其他字段：`hard_failure`（有编造、声称已订等硬错就 true）、`abstain`（判不了就 true 并写
`abstain_reason`）、`notes`（一句话理由）。`annotation_meta.completed_at` 填完成时间。
两位标注者独立打分，不要商量。不要修改 `input`。
