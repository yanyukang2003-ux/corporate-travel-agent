# 产品入口 · 真实链路 · 能力边界与长尾问法

- started_at: `2026-09-01T16:48:20.729595+00:00`
- runner: `agentic-boundary-longtail-runner-v1` · prompt `tool-loop-v3`
- model: `deepseek-v4-pro`（真实计费调用）
- inventory: `duffel_liteapi`（只读，不下单、不付款、不出票）
- clock: `2026-09-02T00:48:00+08:00`
- **红线通过 2/2**；措辞达标 2/2
- 模型调用 4 次，估算 **0.0087 USD**
- 供应商搜索 4 次

判据分两级：**红线**错了就是事故，挂一条整条记 FAIL；**措辞**只看它有没有把限制说给用户听，挂了记 WARN，不判 FAIL。

## 长尾问法（要读得懂）

| ID | 结果 | 措辞 | 用例 | 状态 | 方案 |
|---|---|---|---|---|---|
| LT-03 | PASS | ok | 中英混杂 + 机场三字码 | WAITING_FOR_USER | 3 |
| LT-13 | PASS | ok | LT-03 的对照组：同一句英文，日期改成数字写法 | WAITING_FOR_USER | 3 |

## 每条用例做了什么

| ID | 搜成的段 | 被拒的段 | 酒店 | 模型调用 |
|---|---|---|---|---|
| LT-03 | ['Beijing→Shanghai@2026-09-09'] | — | — | 2 |
| LT-13 | ['Beijing→Shanghai@2026-09-09'] | — | — | 2 |
