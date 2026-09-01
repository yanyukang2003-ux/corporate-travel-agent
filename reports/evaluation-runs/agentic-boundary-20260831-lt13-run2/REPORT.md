# 产品入口 · 真实链路 · 能力边界与长尾问法

- started_at: `2026-08-30T16:51:54.732543+00:00`
- runner: `agentic-boundary-longtail-runner-v1` · prompt `tool-loop-v3`
- model: `deepseek-v4-pro`（真实计费调用）
- inventory: `duffel_liteapi`（只读，不下单、不付款、不出票）
- clock: `2026-08-31T00:51:00+08:00`
- **红线通过 1/3**；措辞达标 1/3
- 模型调用 13 次，估算 **0.0213 USD**
- 供应商搜索 3 次

判据分两级：**红线**错了就是事故，挂一条整条记 FAIL；**措辞**只看它有没有把限制说给用户听，挂了记 WARN，不判 FAIL。

## 长尾问法（要读得懂）

| ID | 结果 | 措辞 | 用例 | 状态 | 方案 |
|---|---|---|---|---|---|
| LT-01 | **FAIL** | WARN | 别称：帝都 / 魔都 | PROVIDER_FAILED | 0 |
| LT-03 | **FAIL** | WARN | 中英混杂 + 机场三字码 | NEEDS_STRUCTURED_INPUT | 0 |
| LT-13 | PASS | ok | LT-03 的对照组：同一句英文，日期改成数字写法 | WAITING_FOR_USER | 3 |

## 红线没过的用例

### LT-01 — 别称：帝都 / 魔都

考的是：口语别称要认得出来，而且'下周三'要自己算。

- **the_legs_the_traveler_named_were_searched**：没搜到 [('Beijing', 'Shanghai')]；实际搜成 []
- **resolved_the_date_the_words_fixed**：少了 ['2026-09-09']；实际搜了 []

用户看到的话：

```
No verified Duffel IATA mapping is configured for location '帝都'
```

### LT-03 — 中英混杂 + 机场三字码

考的是：PEK/SHA 这类三字码要认得出来，英文写的到达时限也要读得对。

- **the_legs_the_traveler_named_were_searched**：没搜到 [('Beijing', 'Shanghai')]；实际搜成 []
- **resolved_the_date_the_words_fixed**：少了 ['2026-09-09']；实际搜了 []

用户看到的话：

```
10 轮内没有收敛到终局动作
```

## 每条用例做了什么

| ID | 搜成的段 | 被拒的段 | 酒店 | 模型调用 |
|---|---|---|---|---|
| LT-01 | — | — | — | 1 |
| LT-03 | — | ['Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai', 'Beijing→Shanghai'] | — | 10 |
| LT-13 | ['Beijing→Shanghai@2026-09-09'] | — | — | 2 |
