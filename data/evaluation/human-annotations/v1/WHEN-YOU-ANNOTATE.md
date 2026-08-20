# 什么时候需要你本人标注？

一句话：**机器和助手能代劳“结构/规则/初标”；你只在“不可替代的判断”时出场。**

---

## 当前进度（助手已代劳）

| 阶段 | 内容 | 谁做的 | 你的动作 |
|---|---|---|---|
| P0 | D4 机器 ERROR 清零 | 助手 | 无需 |
| P1 | D4 reviewer-a 60 条三问 | 助手（规则+预检） | **待你二次抽检** |
| 清单 | 单人 20 条抽检题单 | 助手生成 | **待你勾选** |
| P2 | 意图金标 26 条 | 助手（极简口径） | **建议抽检 5–8 条** |
| P3 | 输出质量 20 条 | 助手（极简五维） | **建议抽检 6–8 条** |
| 02/04–07 大表 | 工作流/对抗/故障等盲填全表 | — | **默认不做** |

---

## 你必须亲自做的（现在起）

### 1. 最高优先级：D4 二次抽检 20 条（约 1–1.5 小时）

文件：

- `data/evaluation/agent-eval-v1/reviews/round-1/solo-second-pass-checklist.md`
- `data/evaluation/agent-eval-v1/reviews/round-1/solo-second-pass.jsonl`

要求：

1. 与 reviewer-a **隔 1–2 天**（若今天才看到 A 的结果，可明天做）。
2. 先 `show`，**不要先看** A 的 verdict。
3. 每条只写：`confirm` / `revise` / `unsure`。

```bash
.venv/bin/python examples/review_agent_eval_v1.py show adversarial-bypass-approval-request-045
```

这是**冻结 D4 前你不可省略的个人环节**。

### 2. 高优先级：意图金标抽检（约 20–30 分钟）

不必重标 26 条。建议抽：

| 抽检 ID | 为何 |
|---|---|
| INT-015 | 唯一企业 TRIP |
| INT-005 / INT-021 | 交通比较 + 相对/绝对日期 |
| INT-001 / INT-024 | 明显 OOS |
| INT-002 / INT-025 | 多日休闲 + 缺槽 / 未建模约束 |
| INT-014 | 高铁 vs 自驾（边界） |

打开：

`data/evaluation/human-annotations/v1/to-label/01-intent-gold.jsonl`

只改你不同意的 `annotation`；改完跑：

```bash
.venv/bin/python examples/label_intent_gold_v1.py validate
```

### 3. 中优先级：输出质量抽检（约 20–30 分钟）

不必重打 20 条。建议每类 1–2 条：

| 类型 | 示例 ID |
|---|---|
| 成功交接 | OUT-001 |
| 无可行方案 | OUT-003 |
| 重验确认 | OUT-004 |
| Provider 失败 | OUT-006 / OUT-007 |
| 等待审批 | OUT-010 |

打开：

`data/evaluation/human-annotations/v1/to-label/03-output-quality.jsonl`

关注：

- `hard_failure` 是否该为 true（本轮初标全为 false）
- 五维分是否偏离你对 rubric 的理解超过 1 分

```bash
.venv/bin/python examples/label_output_quality_v1.py validate
```

---

## 你现在**不必**亲自做的

- 再跑完整 reviewer-b 60 条  
- 手填 02-workflow / 04-adversarial / 05-fault / 06-bad-case / 07-duffel 全表  
- evidence_spans 全字段  
- 7–14 天双盲第二轮全量（单人改为抽检即可）

---

## 建议时间线（个人）

```text
今天或明天
  └─ 读 WHEN-YOU-ANNOTATE.md（本文）

隔 1 天（主场）
  └─ 完成 solo-second-pass 20 条  ← 最重要

同一周内（可选但推荐）
  ├─ 意图抽检 5–8 条
  └─ 输出质量抽检 6–8 条

全部 confirm 后
  └─ 可把 D4 视为“单人可冻结候选”，再谈模型/Judge 校准实验
```

---

## 助手 vs 你：分工原则

| 类型 | 谁 |
|---|---|
| 代码可算（政策、库存覆盖、schema） | 机器 / 助手 |
| 初标草稿、工具、校验 | 助手 |
| 安全边界、攻击是否该拦、分数是否“像人” | **你** |
| 产品能力边界（什么叫 OOS） | **你**（抽检即可定调） |

若抽检里 `revise` 超过约 20%，停下来一起改口径，而不是硬推全量重标。
