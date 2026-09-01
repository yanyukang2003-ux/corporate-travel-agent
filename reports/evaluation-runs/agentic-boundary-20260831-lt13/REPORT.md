# 产品入口 · 真实链路 · 能力边界与长尾问法

- started_at: `2026-08-30T16:49:46.448944+00:00`
- runner: `agentic-boundary-longtail-runner-v1` · prompt `tool-loop-v3`
- model: `deepseek-v4-pro`（真实计费调用）
- inventory: `duffel_liteapi`（只读，不下单、不付款、不出票）
- clock: `2026-08-31T00:49:00+08:00`
- **红线通过 0/2**；措辞达标 1/2
- 模型调用 12 次，估算 **0.0202 USD**
- 供应商搜索 2 次

判据分两级：**红线**错了就是事故，挂一条整条记 FAIL；**措辞**只看它有没有把限制说给用户听，挂了记 WARN，不判 FAIL。

## 长尾问法（要读得懂）

| ID | 结果 | 措辞 | 用例 | 状态 | 方案 |
|---|---|---|---|---|---|
| LT-03 | **FAIL** | WARN | 中英混杂 + 机场三字码 | NEEDS_STRUCTURED_INPUT | 0 |
| LT-13 | **FAIL** | ok | LT-03 的对照组：同一句英文，日期改成数字写法 | WAITING_FOR_USER | 3 |

## 红线没过的用例

### LT-03 — 中英混杂 + 机场三字码

考的是：PEK/SHA 这类三字码要认得出来，英文写的到达时限也要读得对。

- **the_legs_the_traveler_named_were_searched**：没搜到 [('Beijing', 'Shanghai')]；实际搜成 []
- **resolved_the_date_the_words_fixed**：少了 ['2026-09-09']；实际搜了 []

用户看到的话：

```
10 轮内没有收敛到终局动作
```

### LT-13 — LT-03 的对照组：同一句英文，日期改成数字写法

考的是：LT-03 挂在哪一半？把 'Sept 9' 换成 '9/9'，别的一个字不改。过了就说明英文本身没问题，坏的是日期出处关卡只认中文和数字写法。

- **the_legs_the_traveler_named_were_searched**：没搜到 [('Beijing', 'Shanghai')]；实际搜成 [('PEK', 'SHA')]

用户看到的话：

```
推荐 9/9 早上 6:11 起飞的直飞航班，8:12 落地，$69.54——比其它所有选项（$335.80）便宜近 80%，且中午前落地有充足余量。唯一取舍是出发较早。若想晚点出发，9:00→11:15 或 9:30→11:55 的 $335.80 航班也可选，但后者落地仅剩 5 分钟余量。
搜索窗口从 2026-09-08 18:00（PEK 当地）起算，到达时限往前 18 小时，这样前一晚出发也能被搜到；因为你要求 09月09日 12:00 前到达
total_cost=69.54
currency=USD
outbound=off_0000B9u7XQiN92F631QWZe
policy=COMPLIANT
plan_shape=FLIGHT
inventory_snapshots=duffel-flight-4f4670dc8fa34b57
category=best_overall|cheapest
total_cost=335.80
outbound=off_0000B9u7XPSNozrI98wF1B
inventory_snapshots=duffel-flight-2453e50c47d76ecf
category=fastest
outbound=off_0000B9u7XYQaT2llyBAQxk
category=alternative
```

## 每条用例做了什么

| ID | 搜成的段 | 被拒的段 | 酒店 | 模型调用 |
|---|---|---|---|---|
| LT-03 | — | ['PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA', 'PEK→SHA'] | — | 10 |
| LT-13 | ['PEK→SHA@2026-09-09'] | — | — | 2 |
