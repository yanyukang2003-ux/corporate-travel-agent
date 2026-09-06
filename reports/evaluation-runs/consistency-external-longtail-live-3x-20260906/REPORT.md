# 三轮一致性 · 比决策不比文本 · D18 单轮原话 300 条 × 3 轮（2026-09-02 的三轮报告）

- runner: `run-consistency-v1` · generated_at `2026-09-06T15:56:56.104761+00:00`
- 轮次：`external-longtail-live-full-3x-r1-20260902`、`external-longtail-live-full-3x-r2-20260902`、`external-longtail-live-full-3x-r3-20260902`
- 三轮共有用例：300

## 先说名词

| 词 | 大白话 |
|---|---|
| 一致 | 同一句话三轮重跑做了同样的事；这里比的是决策，不比措辞 |
| 终局决策 | 一条用例最后落在哪：追问 / 出方案 / 越界 / 诚实失败 / 供应商失败 |
| 搜索签名 | 一次搜索的路线加旅行者给的到达日；跨日拆窗和被拒的重复搜索得到同一个签名 |
| 追问目标 | 追问在问哪件缺的事（日期、住宿……），用关键词规则读出来，是粗版 |
| 参照 | 报告自带的对错依据：单轮集是离线替身的终态，多轮集是事实表说这条能不能办成 |

## 结论

| 层 | 结果 | 口径 |
|---|---|---|
| 第 1 层 · 终局决策 | 290/300 = 96.7% | 三轮 `state` 相同的用例比例 |
| 第 2 层 · 搜索参数 | 13/13 = 100.0% | 在 3 轮里都搜了的用例中，去重后路线和日期完全相同的比例（去重前 13）；比较方式 `trace_hash` |
| 第 3 层 · 声明的硬要求 | 未计算 | 现有报告没有落盘，要先改 runner 再重跑 |
| 第 4 层 · 追问目标 | 不适用 | 少于两轮带追问原文（单轮集报告里没有 turns） |

## 第 1 层 · 终局决策

| 桶 | 数 | 含义 |
|---|---:|---|
| `consistent_and_matches_reference` | 266 | 三轮相同，且与参照一致（一致且对） |
| `consistent_but_differs_from_reference` | 24 | 三轮相同，但与参照不一致（一致但错，要人看） |
| `mixed` | 10 | 三轮不同 |

各轮终局分布：

| 轮 | NEEDS_CLARIFICATION | NEEDS_STRUCTURED_INPUT | NO_FEASIBLE_OPTION | OUT_OF_SCOPE | PROVIDER_FAILED | WAITING_FOR_USER |
|---|---:|---:|---:|---:|---:|---:|
| `external-longtail-live-full-3x-r1-20260902` | 270 | 0 | 0 | 21 | 4 | 5 |
| `external-longtail-live-full-3x-r2-20260902` | 272 | 0 | 1 | 18 | 4 | 5 |
| `external-longtail-live-full-3x-r3-20260902` | 272 | 1 | 1 | 20 | 3 | 3 |

三轮不同的 10 条：

| case_id | external-longtail-live-full-3x-r1-20260902 | external-longtail-live-full-3x-r2-20260902 | external-longtail-live-full-3x-r3-20260902 |
|---|---|---|---|
| `ct-human-h20241029143455115600` | OUT_OF_SCOPE | OUT_OF_SCOPE | NEEDS_CLARIFICATION |
| `ct-human-h20241029143508251643` | OUT_OF_SCOPE | NEEDS_CLARIFICATION | NEEDS_CLARIFICATION |
| `ct-human-h20241029143542560410` | NEEDS_CLARIFICATION | WAITING_FOR_USER | NEEDS_CLARIFICATION |
| `ct-human-h20241029143546424651` | NEEDS_CLARIFICATION | NEEDS_CLARIFICATION | OUT_OF_SCOPE |
| `ct-human-h20241029143630351554` | WAITING_FOR_USER | WAITING_FOR_USER | NEEDS_STRUCTURED_INPUT |
| `ct-human-h20241029143732290071` | OUT_OF_SCOPE | NEEDS_CLARIFICATION | NEEDS_CLARIFICATION |
| `ct-human-h20241029143735439292` | NEEDS_CLARIFICATION | NEEDS_CLARIFICATION | OUT_OF_SCOPE |
| `ct-human-h20241029143738057260` | PROVIDER_FAILED | PROVIDER_FAILED | NEEDS_CLARIFICATION |
| `ct-human-h20241029143818664124` | WAITING_FOR_USER | NO_FEASIBLE_OPTION | NO_FEASIBLE_OPTION |
| `cw-10351` | OUT_OF_SCOPE | NEEDS_CLARIFICATION | OUT_OF_SCOPE |

## 第 2 层 · 搜索参数

| 桶 | 数 |
|---|---:|
| `searched_in_some_compared_runs_only` | 25 |
| `identical` | 13 |
| `search_presence_mixed`（比较的轮次里有的搜了有的没搜） | 25 |

被宿主拒掉的搜索尝试（`ToolInputError`，不算搜了）：

| 轮 | 次数 | 涉及用例 |
|---|---:|---:|
| `external-longtail-live-full-3x-r1-20260902` | 109 | 91 |
| `external-longtail-live-full-3x-r2-20260902` | 105 | 85 |
| `external-longtail-live-full-3x-r3-20260902` | 115 | 91 |

## 第 4 层 · 追问目标（粗版）

少于两轮带追问原文（单轮集报告里没有 turns）

## 局限

- 第 2 层只比路线和日期，不比到达时限的具体时刻、席别过滤等更细的参数；地点按模型送出的原文比，同一地方一轮写城市名一轮写三字码（New York / JFK）会记成路线不同。
- 第 4 层是关键词规则，不一致用例里会混有漏判；精确版要 runner 把 `open_questions` 结构化落盘。
- 第 3 层（声明的硬要求）没有数据；多轮集里「办成与否不一致」的根因正在这一层。
- 一致不等于对：`consistent_but_differs_from_reference` 那一桶要单独看。

## 输入指纹

| 轮 | report.json sha256 | traces.jsonl sha256 | 搜索参数 | 轨迹 | 追问原文 |
|---|---|---|---|---|---|
| `external-longtail-live-full-3x-r1-20260902` | `57bbbaabca2271f9…` | `9edfa7071d872237` | 无 | 有 | 无 |
| `external-longtail-live-full-3x-r2-20260902` | `aaf30c0f5c11848b…` | `95e1f9c8a425dd9a` | 无 | 有 | 无 |
| `external-longtail-live-full-3x-r3-20260902` | `b4dd3abfb349654c…` | `1f0dab259083155c` | 无 | 有 | 无 |
