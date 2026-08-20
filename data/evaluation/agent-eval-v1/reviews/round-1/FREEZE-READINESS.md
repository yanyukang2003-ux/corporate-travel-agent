# D4 agent-eval-v1 单人冻结就绪清单

更新：2026-08-09  
评审模式：单人（reviewer-a 全量 + 二次抽检 20 条，不强制 reviewer-b）  
**冻结状态：已冻结（owner 确认 A）**

## 已完成

| 项 | 状态 | 证据 |
|---|---|---|
| 机器 audit ERROR=0 | ✅ | `review_agent_eval_v1.py audit --status error` |
| reviewer-a 60 条 | ✅ | `reviews/round-1/reviewer-a.json`（accept 60） |
| 单人二次抽检 20 条 | ✅ | `solo-second-pass.jsonl` 全部 `confirm` |
| 默认出发时间 | ✅ | `departure_after` / 去程航班默认 **08:00+08:00** |
| 默认下午返程窗 | ✅ | **13:00–18:00+08:00**；回程库存已对齐 |
| 相关单测 | ✅ | `test_agent_eval_v1_*` |
| D4 executor 60/60 | ✅ | oracle + deterministic_live |
| 24 条模型冒烟子集 | ✅ | `agent-eval-model-smoke-v1` + `--subset` |
| **manifest frozen** | ✅ | `status=frozen`，`dataset_version=1.0.3`，`frozen_at=2026-08-09` |
| C2 model_mock runner | ✅ | `run_agent_eval_model_smoke.py`；fixture 24/24 |

## 冻结字段摘要

```text
status: frozen
dataset_version: 1.0.3
review.human_reviewed_records: 60
review.review_mode: solo_second_pass
review.solo_second_pass: 20 confirm
review.reviewer_a: 60 accept
cases[].provenance.human_reviewed: true
```

`1.0.1` 是不改标签的夹具修订：case 024 补齐 8 月 23 日返程库存，case 044 的首轮铺垫与 no-hotel oracle 对齐。`1.0.2` 将 case 044 的到达 oracle 对齐用户明确给出的 `10:00-07:00`，并移除不存在的会议约束。`1.0.3` 移除 case 004/012 中没有用户依据的返程：出差或酒店日期区间本身不再被视为返程请求。

## 仍为 soft / 非阻塞

| 项 | 说明 |
|---|---|
| WARN ×49 `FIXED_SIBLING_SEARCH_ORDER` | 搜索工具顺序写死；人工已接受可不改 |
| 真实 LLM 计费跑分 | runner 已就绪；需 `--confirm-billable` + API key |
| 系统事件全剧本 | live 60/60 靠驱动器+fixture，事件驱动可更完整 |

## 命令备忘

```bash
.venv/bin/python examples/review_agent_eval_v1.py audit --status error
.venv/bin/python examples/build_agent_eval_v1.py --check
.venv/bin/python examples/run_agent_eval_v1.py --mode deterministic_live \
  --subset evals/subsets/agent-eval-model-smoke-v1.json

# C2 model_mock（fixture 不计费见 pytest；真实 LLM 需 confirm-billable）
.venv/bin/python examples/run_agent_eval_model_smoke.py \
  --subset evals/subsets/agent-eval-model-preflight-v1.json \
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \
  --model "$OPENAI_MODEL" --confirm-billable \
  --output reports/evaluation-runs/d4-model-preflight
```

**注意：** 重新运行 `build_agent_eval_v1.py`（无 `--check`）会重写 cases/worlds/manifest；
改 SPECS 后必须重建，并重算 subset 的 `source_*_sha256`。
