# agent-eval-v1（D4）

状态：`frozen`  
版本：`1.0.0`  
冻结日：`2026-08-09`  
评审模式：单人（reviewer-a 全量 accept 60 + solo-second-pass 20 confirm）

这是 Corporate Travel Agent 的 60 条状态驱动端到端评测集。案例不要求模型复现唯一
工具轨迹，而是检查最终状态、政策结论、必要工具偏序、参数来源、禁止行为和用户披露。

## 文件

- `cases.jsonl`：60 条评测案例，每行一个符合
  `evals/schemas/evaluation-case.schema.json` 的 JSON 对象。
- `fixtures/worlds.json`：以 `case_id` 为键的冻结世界状态，包含员工、政策、请求真值、
  Replay 库存、重验证结果、handoff、故障载荷和执行预算。
- `manifest.json`：数量、分层、哈希、覆盖统计和已知限制。
- `examples/build_agent_eval_v1.py`：确定性生成与校验脚本。

## 构成

| 类别 | 数量 | 目的 |
|---|---:|---|
| `core` | 16 | 合规、审批、禁止、证据不足、澄清、越界、handoff |
| `historical_failure` | 12 | 涨价、售罄、Provider 失败、部分结果、陈旧快照、重试、审批失效 |
| `boundary` | 16 | 金额、到达缓冲、日期、政策/审批/库存有效期、工具预算、时区边界 |
| `adversarial` | 16 | 绕过审批、伪造授权、提示注入、身份冒用、隐私、价格伪造、重放 |

Split 固定为 40 条 `development` 和 20 条 `regression`。51 条中文、9 条英文；32 条为
`critical` 风险。

## 校验

从项目根目录执行：

```bash
.venv/bin/python examples/build_agent_eval_v1.py --check
```

重新生成会覆盖 D4 的三个生成文件，因此只能在修改生成脚本后执行：

```bash
.venv/bin/python examples/build_agent_eval_v1.py
```

## 简化人工评审（推荐）

不要直接阅读完整的 `cases.jsonl` / `worlds.json`，也不要手工编辑 reviewer JSON。
使用评审助手把每条案例压缩成三组明确对比，并让命令行统一写入评审结果：

```bash
# 第一次执行：升级/初始化统一格式的评审文件
.venv/bin/python examples/review_agent_eval_v1.py init --reviewer reviewer-a

# 先集中查看机器可确定的问题；修复成批问题后再开始人工评审
.venv/bin/python examples/review_agent_eval_v1.py audit

# 查看下一条 pending 案例（只读）
.venv/bin/python examples/review_agent_eval_v1.py next --reviewer reviewer-a

# 交互评审下一条；回答三个问题后自动保存 JSON
.venv/bin/python examples/review_agent_eval_v1.py review --reviewer reviewer-a

# 查看进度和校验文件格式
.venv/bin/python examples/review_agent_eval_v1.py status --reviewer reviewer-a
.venv/bin/python examples/review_agent_eval_v1.py validate --reviewer reviewer-a
```

评审助手使用三个互相独立的判断基准：

1. `turns` 是意图/参数标签的证据，人工只判断用户是否真的表达了这些值；
2. 项目中的可行性、排序和政策代码计算 `request + employee + policy + inventory`，用来核对
   最终状态与政策结论；`world.expected_oracle` 只做内部一致性检查，不作为正确性证据；
3. 案例声称要防止的错误行为是 hard assertions 的基准，人工判断断言是否真的覆盖了目标。

脚本会自动执行内部标签一致、安全边界、政策计算、工具预算、重验覆盖、最低价选择断言
等预检。人工评审文件采用
`evals/schemas/agent-eval-human-review.schema.json` 的固定 v2 格式，每条案例只保留三个
`pass/fail/unsure`、问题代码和备注。

评审 B 使用相同命令但将 reviewer 改为 `reviewer-b`。两人独立完成后运行：

```bash
.venv/bin/python examples/review_agent_eval_v1.py compare
.venv/bin/python examples/review_agent_eval_v1.py prepare-adjudication
.venv/bin/python examples/review_agent_eval_v1.py adjudicate
.venv/bin/python examples/review_agent_eval_v1.py final-check
```

校验器覆盖当前案例 Schema 使用的结构、类型、枚举、必填字段、唯一性和数量约束，另检查：

- 60 个 `case_id` 与 60 个 world 一一对应；
- 16/12/16/16 类别和 40/20 split 不漂移；
- 必须工具全部来自冻结工具注册表 v2；
- 必须工具和禁止工具不冲突；
- 每条案例都禁止真实下单和支付；
- fixture JSON Pointer 指向本案例的 world；
- `cases.jsonl` 与 `worlds.json` 哈希匹配 manifest。

## 运行硬断言基线（D4 executor）

```bash
# 标签一致性烟测（executor 接线 + 断言路径）：应 60/60
.venv/bin/python examples/run_agent_eval_v1.py --mode oracle_label \
  --output reports/evaluation-runs/d4-agent-eval-oracle-latest

# 确定性 live：真实编排器 + world fixture（当前基线 60/60）
.venv/bin/python examples/run_agent_eval_v1.py --mode deterministic_live \
  --output reports/evaluation-runs/d4-agent-eval-baseline-latest

# 单条
.venv/bin/python examples/run_agent_eval_v1.py --mode deterministic_live \
  --case-id core-compliant-round-trip-001
```

实现：`src/corporate_travel_agent/services/evaluation_agent_eval.py`  
- fixture loader（cases/worlds/manifest 哈希）  
- hard-assertion executor（equals/exists/contains/tool_order/forbidden tools）  
- `deterministic_live` 驱动 `TripWorkflowOrchestrator`；无 `request_oracle` 时回退 oracle 观测  

## 冻结状态（已完成）

1. ~~两名评审~~ → 单人：`reviewer-a` + `solo-second-pass` 20 confirm（见 `reviews/round-1/`）。
2. ~~fixture loader + hard-assertion executor~~ → 已落地；系统事件完整驱动仍可加深。
3. ~~提高 live 基线~~ → deterministic_live **60/60**。
4. ~~固定 24 条中英模型冒烟子集~~ → `evals/subsets/agent-eval-model-smoke-v1.json`（确定性 hard-assertion 已接）。
5. ~~人工确认后 frozen~~ → `manifest.json` 已为 `status=frozen` / `dataset_version=1.0.3`。

**仍可选：** 真实 LLM 在环跑 24 条 smoke（C2）；系统事件全剧本加深。

固定 24 条冒烟：

```bash
.venv/bin/python examples/run_agent_eval_v1.py --mode deterministic_live \
  --subset evals/subsets/agent-eval-model-smoke-v1.json \
  --output reports/evaluation-runs/d4-smoke-live-latest
```

当前数据全部是合成 Replay world，不能证明真实 Provider 的库存质量或网络稳定性。
