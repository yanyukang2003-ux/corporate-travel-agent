# Intent Gold Round 1（simplified_v1）

完成时间：与 `01-intent-gold.jsonl` 中 `annotation_meta.completed_at` 一致。  
标注风格：极简（不写 evidence_spans；只标可支撑槽位）。

## 分布

| classification | 数量 | 说明 |
|---|---:|---|
| OUT_OF_SCOPE | 10 | 观光路线/POI/单日打卡/开放目的地闲聊 |
| MULTI_DAY_TRIP | 11 | 多日休闲或开放行程；多条需澄清日期/目的地 |
| TRANSPORT_COMPARE | 4 | 高铁/飞机或高铁/自驾比较 |
| TRIP | 1 | 企业差旅 LHR→JFK（INT-015） |
| NEEDS_CLARIFICATION | 0 | 任务类型均可归类；缺槽位用 missing + must_clarify |

## 相对日期约定

- 多数案例 `reference_time=2026-07-20T09:00:00Z`，`timezone=Asia/Shanghai`（周一 17:00）。
- 「明天」→ `2026-07-21T00:00:00+08:00`
- 「下周二」→ `2026-07-28T00:00:00+08:00`
- 「7月26号 / 8月3日」无年份时取 2026

## 校验

```bash
.venv/bin/python examples/label_intent_gold_v1.py validate
```

## 与后续步骤

- 本文件只覆盖 **01-intent-gold**。
- 03-output-quality 仍待 P3（Judge 校准 20 条）。
- 02/04–07 按先前策略不强制盲填全表。
