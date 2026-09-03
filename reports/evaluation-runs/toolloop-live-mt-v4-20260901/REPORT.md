# 工具循环 · 真实多轮（缺信息 / 语序颠倒 / 过期改口）

- started_at: `2026-09-01T23:11:11.242567+00:00`
- runner: `tool-loop-live-multiturn-runner-v1` · prompt `tool-loop-v4`
- model: `deepseek-v4-pro`
- inventory: `duffel_liteapi`（只读，不下单）
- clock: `2026-08-19T15:00:00+08:00`
- result: **6/6 PASS**
- 模型调用: 23 次，估算 **0.04385409 USD**
- 供应商搜索: 18 次

| ID | 类 | Result | Title |
|---|---|---|---|
| LM-01 | missing | PASS | 信息缺少：先说要出差，城市和日期分两轮再补 |
| LM-02 | missing | PASS | 信息缺少：第一段有日期，后面两段先问再补 |
| LM-03 | reversed | PASS | 语序颠倒：先说回程，再说中间，最后才说去程 |
| LM-04 | reversed | PASS | 语序颠倒：第一轮只说回程，第二轮才补去程和中间段 |
| LM-05 | stale | PASS | 过期信息：先给已经过去的 8月5号，再改口成 9月15号 |
| LM-06 | stale | PASS | 错误信息：先说去广州，第二轮改口去上海 |

## 每条用例最终读数

| ID | 状态 | 方案 | 航段 |
|---|---|---|---|
| LM-01 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai')] |
| LM-02 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai'), ('Shanghai', 'Hangzhou'), ('Hangzhou', 'Beijing')] |
| LM-03 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai'), ('Shanghai', 'Hangzhou'), ('Hangzhou', 'Beijing')] |
| LM-04 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai'), ('Shanghai', 'Hangzhou'), ('Hangzhou', 'Beijing')] |
| LM-05 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai')] |
| LM-06 | WAITING_FOR_USER | 3 | [('Beijing', 'Shanghai')] |
