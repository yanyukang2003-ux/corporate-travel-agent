# Session Handoff — Corporate Travel Agent

**日期：** 2026-08-28  
**工作区：** `/Users/yukangyan/Downloads/corporate-travel-agent`  
**分支：** `semantic-entrypoint-and-judge`（工作树干净）  
**目的：** 换 session 续作入口。**读 §1 和 §30 就能接上**，其余章节是历史记录，按需查。

---

## 1. 现在是什么状态

### 1.1 一分钟版

系统能用大白话接需求 → 读懂 → 查真实航班酒店 → 按公司规定判合规 → 需要时走审批 →
交给员工自己去官方平台下单。**不自动下单、不付钱**，这是硬边界不是没做完。

**这几轮在做的事：** 把它从「一张会说话的搜索表单」改造成「会规划的助手」。
**架构方案与六步计划见 §30**，前三步已落地。

### 1.2 现在能做什么 / 不能做什么

| | 状态 |
|---|---|
| 单程、原路往返 | ✅ |
| **开口程**（去上海、从杭州回） | ✅ **§29 刚支持**，真实模型实测 2/2 |
| 多城（三段以上） | ❌ 领域模型能表达（上限 6 段），但**语义层还只产出 1–2 段**；会被正确拦住不搜 |
| 多轮对话逐步搭行程 | ✅ 真实模型 5/5（每条 2 次） |
| 中文日期边角 | ✅ 真实模型 8/8（每条 3 次） |
| 自动下单 / 付款 | ❌ **永远不做**，硬边界 |
| 儿童票、签证、选座、里程卡、已出票改签、宠物、无障碍 | ❌ 明确的能力边界，会披露且不搜 |

### 1.3 关键路径与入口

- **前端新建任务走 `/semantic/trip-tasks`**（§24 已切换）；历史 legacy 任务继续走旧路由
- 旧链路保留为 ADR-0002 的回滚保险，**不可从语义任务到达**
- 真实模型：DeepSeek `deepseek-v4-pro`；Provider：Duffel + LiteAPI **沙箱只读**

### 1.4 验收基线

```
pytest 646 / ruff / 前端 build+test+lint     全过
红队 runner（语义入口）                       17/17
日期边角 · 真实模型 · 每条 3 次                8/8
多轮 + 多段行程 · 真实模型 · 每条 2 次          5/5
LLM Judge 真跑 60 条                          均分 4.55，邻近一致 97.6%
Postgres 双 worker 并发                       2/2
```

### 1.5 待项目所有者拍板（按紧急程度）

1. **城市别名表补全** —— 已经第三次咬人（大阪、广州、杭州）。`config/travel-policy.json`
   只有 10 个城市，表外城市**半英半中地继续跑且不报错**，真实 Provider 能否查询未验证
2. **产品定位** —— 是「会规划的助手」还是「更聪明的搜索表单」？**这决定 §30 的第 5、6 步做不做**
3. **往返是否作为一次多段报价请求** —— 现在每段单独搜，等于两张单程票（详见 §26）
4. CI 评测门禁的花钱策略（合并时跑 / 每晚跑）
5. Judge 人工双评的第二个标注者

---

以下为 2026-08-19 及更早的状态，仍然有效：

**§18.3 A–I 已打完。不要再堆缺槽闲聊，也不要重复 A–I。**  
D4 frozen v1.0.3；DeepSeek smoke 24/24、24×3 全过；Duffel 重验 / D14 Test Order 历史基线仍有效。  
Intake 修复已进代码：跳年、条件酒店、闲聊不 OOS、预算后仍搜索、二选一城市、对比清互斥舱位、换目的地重锚时间。  
出方案后改口已进代码：改单程、酒店不要了、订两晚、会议改点、返程改后天晚上、无「改为」改出发地。  
日历边角已进代码：下下周三、这周五还是下周五、8/5 / 8.5 / 2026.8.5、春节不编公历、12/30→1/2 跨年。  
能力边界已进代码：模型判断用户是否在要这些能力，宿主强制披露且不搜；无模型时才用正则兜底。  
**尚未测：** §18.3 J D6 回流 / D4 24 题计费复测。

---

## 2. 本会话主线做了什么

按 §18.3 **I** 做能力边界披露。没有补缺槽闲聊，没有重跑 D14，没有计费 smoke，没有重复 A–H。

1. 读 HANDOFF，确认 A–H 已完，下一优先是 I / D6  
2. **I 能力边界**：儿童/婴儿、签证护照、选座/里程卡、开口票/缺口程/多城、已出票改签退票、无障碍/宠物  
3. 能力边界改分层：模型填 `unsupported_capabilities`，宿主强制披露+禁搜；成功抽取且列表为空则信任模型（如「签证中心」当会面地点）  
4. 无模型/抽取失败才用用户文本正则兜底，避免 LLM 宕机时装满足  
5. 验收：`examples/run_capability_boundary_acceptance.py` **8/8**；报告见 §22  

更早会话（A–H、DeepSeek 24/24、Duffel 重验、D14）仍有效。

---

## 3. 关键事实与路径

### 数据集 / 子集

| 项 | 状态 / 路径 |
|---|---|
| D4 根 | `data/evaluation/agent-eval-v1/` **frozen 1.0.2** |
| cases / worlds | `cases.jsonl`，`fixtures/worlds.json` |
| 24 smoke | `evals/subsets/agent-eval-model-smoke-v1.json` |
| 2 preflight | `evals/subsets/agent-eval-model-preflight-v1.json` |
| D13 Duffel 重验 | `evals/subsets/duffel-real-revalidation-smoke-v1.json` **frozen 1.0.0** |
| D14 Duffel Test Order | `evals/subsets/model-duffel-test-order-e2e-v1.json` **frozen 1.0.0** |
| 冻结说明 | `reviews/round-1/FREEZE-READINESS.md` |

### 代码（评测 + 意图）

| 组件 | 路径 |
|---|---|
| D4 hard-assert runner | `src/corporate_travel_agent/services/evaluation_agent_eval.py` |
| 确定性 / model_mock CLI | `examples/run_agent_eval_v1.py`，`examples/run_agent_eval_model_smoke.py` |
| **P0/P1 校准 + 防编造** | `src/corporate_travel_agent/agent/intent_calibration.py` |
| **Claude Code 参数环（本地化）** | `src/corporate_travel_agent/agent/param_extraction_loop.py` |
| 抽取环（L1→L2→tool_use_error→REPAIR/CLARIFY） | `orchestrator._extract_and_continue` |
| LLM 适配 | `agent/openai_adapter.py` **prompt_version=`trip-intent-v3`** |
| D14 runner / 证据恢复 | `examples/run_real_model_duffel_test_order.py`，`examples/recover_duffel_test_order_evidence.py` |
| Duffel Test Order 客户端 | `src/corporate_travel_agent/providers/duffel_test_order.py` |
| **§18.3 A–E 验收** | `examples/run_uncovered_types_acceptance.py` → `reports/evaluation-runs/uncovered-types-20260819/` **23/23** |
| **§18.3 F / §16.2 PG** | `examples/run_postgres_dual_worker_acceptance.py` → `reports/evaluation-runs/postgres-dual-worker-20260819/` **PASS** |
| **§18.3 G 改口** | `examples/run_revision_knives_acceptance.py` → `reports/evaluation-runs/revision-knives-20260819/` **6/6** |
| **§18.3 H 日历** | `examples/run_calendar_edge_acceptance.py` → `reports/evaluation-runs/calendar-edge-20260819/` **8/8** |
| **§18.3 I 能力** | `examples/run_capability_boundary_acceptance.py` → `reports/evaluation-runs/capability-boundary-20260819-model/` **8/8** |
| 相关单测 | `tests/test_param_extraction_loop.py`，`tests/test_intent_calibration.py`，`tests/test_intent_scenarios.py`，`tests/test_local_intent.py`，`tests/test_evaluation_agent_eval.py` |

### 环境

```bash
# 密钥在仓库根 .env（勿提交）
set -a && source .env && set +a
# 常用：OPENAI_API_KEY、OPENAI_MODEL=deepseek-v4-pro
#       OPENAI_BASE_URL=https://api.deepseek.com（DeepSeek OpenAI 兼容）
# 价格表：evals/pricing/model-prices-openai-20260802-v1.json
#   deepseek-v4-pro: $0.435/1M in + $0.87/1M out（cache miss 上界）
```

---

## 4. 真实 LLM 跑分账本（务必保留）

| 运行 | 通过 | LLM 次 | 估算 USD | Token in/out | 报告目录 |
|---|---|---:|---:|---|---|
| preflight r3 | 2/2 | 2 | 未 ledger | — | `reports/evaluation-runs/d4-model-preflight-20260808-r3/` |
| smoke baseline 24×1 | 20/24 (83.3%) | 26 | 未 ledger | — | `.../d4-model-smoke-20260808/` + `BASELINE.md` |
| stability 24×3 | pass_power_3 20/24；mixed 1/24 | 78 | $0.495120 | 93924 / 25606 | `.../d4-model-smoke-stability-24x3-20260808/` + `STABILITY.md` |
| **DeepSeek v1.0.2 stability 24×3** | **pass_power_3 24/24；mixed 0/24** | **108** | **$0.120504** | **204426 / 36297** | **`.../d4-model-stability-deepseek-v102-20260809-rerun-1/` + `STABILITY.md`** |
| postfix（作废） | 16/24 | 26 | $0.174494 | 33487 / 8960 | `.../d4-model-smoke-20260808-postfix/` |
| postfix2 | 21/24 (87.5%) | 26 | $0.163926 | 33261 / 8117 | `.../d4-model-smoke-20260808-postfix2/` + `POSTFIX.md` |
| P0/P1 smoke 24×1 | 22/24 (91.7%) | 26 | $0.165790 | 34925 / 7995 | `.../d4-model-smoke-p01-20260808-144630/` + `BASELINE.md` |
| 参数环 smoke 24×1 | 21/24 (87.5%) | 26 | $0.172738 | 34925 / 8574 | `.../d4-model-smoke-paramloop-20260808-150843/` + `BASELINE.md` |
| **deepseek-v4-pro smoke** | **19/24 (79.2%)** | **39** | **$0.0422** | **71031 / 13043** | **`.../d4-model-smoke-deepseek-20260809-113525/` + `BASELINE.md`** |
| **deepseek-v4-pro 最终修复基线** | **24/24 (100%)** | **36** | **$0.040306** | **68142 / 12258** | **`.../d4-model-smoke-deepseek-20260809-final-rerun-3/` + `BASELINE.md`** |
| deepseek preflight | 2/2 | 3 | $0.0034 | 5467 / 1174 | `.../d4-model-preflight-deepseek-20260809-113506/` |
| **DeepSeek + Duffel D12 1×3** | **3/3；pass^3=1；mixed=0** | **3（Duffel 3）** | **$0.003036** | **5739 / 620** | **`.../deepseek-duffel-full-recovery-v102-20260809/`** |
| **Duffel D13 Offer 重验** | **19/19 checks** | **0（Duffel HTTP 2）** | **$0** | **—** | **`.../duffel-revalidation-smoke-20260809/`** |
| **Duffel D13 重验 1×3** | **3/3；pass^3=1；mixed=0；12/12 checks** | **0（Duffel HTTP 6）** | **$0** | **—** | **`.../duffel-revalidation-stability-20260809/`** |
| **D14 Test Order** | **外部交易 PASS；正式评测 INVALID；证据 15/15** | **1（Duffel HTTP 7）** | **usage 未落盘** | **未落盘** | **`.../deepseek-duffel-test-order-e2e-20260809/`** |
| **D14 Test Order 正式重跑** | **28/28 PASS；订单已取消** | **1（Duffel HTTP 7）** | **$0.001008** | **1909 / 204** | **`.../deepseek-duffel-test-order-e2e-formal-20260809-rerun-1/`** |
| **§18.3 A–E 直播验收** | **23/23 PASS** | **0 DeepSeek**（8010 mock）；E-recover 为 Duffel+LiteAPI **只读** | **$0 模型** | **—** | **`.../uncovered-types-20260819/`** |
| **§18.3 F PG 双 worker** | **PASS**（1 PID Provider；EXPLAIN 走索引） | **0** | **$0** | **—** | **`.../postgres-dual-worker-20260819/`** |
| **§18.3 G 改口剩余刀** | **6/6 PASS** | **0**（scripted + Mock） | **$0** | **—** | **`.../revision-knives-20260819/`** |
| **§18.3 H 日历边角** | **8/8 PASS** | **0**（scripted + Mock） | **$0** | **—** | **`.../calendar-edge-20260819/`** |
| **§18.3 I 能力边界** | **8/8 PASS** | **0**（scripted + Mock） | **$0** | **—** | **`.../capability-boundary-20260819-model/`** |

每次计费跑产物：`agent-eval-v1-run-summary.json`、`failed-assertions.jsonl`、`cost-ledger.jsonl`、`cost-summary.json`、`model-run-meta.json`。

D14 的 1 个 `live_mode=false` Test Order 已于 2026-08-09T11:16:05Z 完成取消，
USD 231.37 已退回 Test Mode balance。恢复报告只验证 7 份 Duffel 归档响应，不补造缺失的
model usage 或 workflow trace，因此不得视为正式评测 PASS。当时的正式重跑必须重新取得
创建另一个 Test Order 的明确授权；该授权随后已单独取得并只使用一次。

随后取得的新授权只用于一次正式重跑。该轮 28/28 PASS，第二个 `live_mode=false` Test
Order 同样已取消，USD 221.85 已退回 Test Mode balance；模型 usage、21 步 trace、结果和
7 份归档响应均已落盘。首次 INVALID 运行继续保留作故障审计。

### P0/P1 smoke 相对 postfix2

| 题 | postfix2 | P0/P1 smoke |
|---|---|---|
| `boundary-arrival-buffer-exact-031` | fail (`NEEDS_CLARIFICATION`) | **pass** (`READY_FOR_HANDOFF`) |
| `core-unknown-level-policy-012` | pass | **pass** |
| `historical_failure-empty-return-inventory-027` | fail | fail（仍停在澄清） |
| `boundary-timezone-cross-date-044` | fail | fail（仍停在澄清） |

### 最新稳定性指标（24×3，v1.0.2 最终修复路径）

| 指标 | 值 | 含义 |
|---|---|---|
| pass_power_3 | **24/24（100%）** | 三轮全过的题数（主稳定性） |
| pass_at_3 | 24/24（100%） | 至少过 1 次 |
| mixed_run_rate | **0/24（0%）** | 有过有挂 |
| runner error | 0 | 72 轮均完成 |

旧 v1.0.0 路径结果为 pass_power_3 20/24、mixed 1/24；仅保留作历史对照。

### 硬断言 pass 定义（model_mock）

一次 attempt **pass** = 全部 hard_assertions 过且无 runner error。  
不是「回复像人」，而是终态/政策/工具序/安全等尺子。

---

## 5. P0/P1 意图校准（防编造）— 已验证有效

**最新真实 DeepSeek 24×1 已复测：24/24。**  
**Claude Code 参数提取标准环已本地化**（`param_extraction_loop.py`，单测绿；最新 24×3 计费稳定性已覆盖）。

```text
模型抽取（Structured Outputs + extra=forbid）
  → 反编造 merge（只信 provided_fields；repair 只许补 missing）
  → P0 白名单派生（从已有字段推时间/酒店成对）
  → L1 schema validate（类型/时区；Claude safeParse 对应）
  → L2 business validateInput（SearchReady + domain；Claude validateInput 对应）
  → 失败 → format tool_use_error（Claude formatZodValidationError 文案）
       → REPAIR ≤2（仅日期/酒店 + 错误回灌）| CLARIFY（城市/冲突/预算尽）
  → ACCEPT → 搜索/政策
```

| 规则 | 行为 |
|---|---|
| 不编城市 | origin/destination 缺失 → **只澄清**，不做 LLM silent repair |
| P0 默认 | 有 arrive_by → 同日 08:00 出发；返程/酒店**成对**补全；assumption 带 `policy_default:` |
| P1 repair | 只补 `departure_after` / 返程窗 / 酒店日；拒绝改写已填槽 |
| 审计 | `task.metadata["intent_calibration"]` |

**概念：选方案不进澄清** = 状态已是 `NEEDS_CLARIFICATION` 时，不要把「选第一名/交审批」当 `submit_message` 补槽（会污染意图）。

---

## 6. model_mock 驱动约定（多轮真实 LLM）

Runner：`d4-agent-eval-model-mock-v2-sequential-turns`

```text
第 1 句用户话  → create_task_from_message → LLM extract #1 → 搜索/澄清
第 2、3… 句：
   · 「选方案」类     → select_option（不调 LLM）
   · system_event     → 审批/时钟等（不调 LLM）
   · 其它任意用户话   → submit_message → LLM extract #N
```

- **`planning_intent_message`：永远只取第一句**；后面全部进 `remaining`（不再拼进首轮 prompt）  
- 允许再调 LLM 的状态白名单：`NEEDS_CLARIFICATION` / `NEEDS_STRUCTURED_INPUT` /
  `WAITING_FOR_USER` / `NO_FEASIBLE_OPTION` / `WAITING_FOR_PROVIDER` / `PROVIDER_FAILED`  
- `OUT_OF_SCOPE` 仍是终态；`TOOL_BUDGET_EXHAUSTED` 也不会继续调用模型  
- Mock provider + world fixture；硬断言打分

---

## 7. 常用命令

```bash
cd /Users/yukangyan/Downloads/corporate-travel-agent
set -a && source .env && set +a

# 确定性
.venv/bin/python examples/build_agent_eval_v1.py --check
.venv/bin/python examples/run_agent_eval_v1.py --mode deterministic_live \
  --output reports/evaluation-runs/d4-live-check

# 单测（含 P0/P1）
.venv/bin/python -m pytest tests/test_intent_calibration.py tests/test_intent_scenarios.py \
  tests/test_evaluation_agent_eval.py -q

# 真实 LLM smoke 24×1（计费；新目录勿预先 mkdir）
.venv/bin/python examples/run_agent_eval_model_smoke.py \
  --subset evals/subsets/agent-eval-model-smoke-v1.json \
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \
  --model "$OPENAI_MODEL" \
  --confirm-billable \
  --output reports/evaluation-runs/d4-model-smoke-p01-$(date +%Y%m%d-%H%M%S)

# 稳定性 24×3
.venv/bin/python examples/run_agent_eval_model_smoke.py \
  --attempts 3 \
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \
  --model "$OPENAI_MODEL" \
  --confirm-billable \
  --output reports/evaluation-runs/d4-model-stability-p01-$(date +%Y%m%d-%H%M%S)

# §18.3 A–E（独立 8010 mock+AUTH；output 目录必须不存在）
.venv/bin/python examples/run_uncovered_types_acceptance.py \
  --confirm-external-test-calls \
  --output reports/evaluation-runs/uncovered-types-$(date +%Y%m%d-%H%M%S)

# §18.3 F Postgres 双 worker（Colima + docker-compose；勿用业务库）
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
.venv/bin/python examples/run_postgres_dual_worker_acceptance.py \
  --output reports/evaluation-runs/postgres-dual-worker-$(date +%Y%m%d-%H%M%S)

# §18.3 G 出方案后改口（进程内 scripted+Mock；output 目录必须不存在）
.venv/bin/python examples/run_revision_knives_acceptance.py \
  --output reports/evaluation-runs/revision-knives-$(date +%Y%m%d-%H%M%S)

# §18.3 H 日历边角
.venv/bin/python examples/run_calendar_edge_acceptance.py \
  --output reports/evaluation-runs/calendar-edge-$(date +%Y%m%d-%H%M%S)

# §18.3 I 能力边界（进程内 scripted+Mock；output 目录必须不存在）
.venv/bin/python examples/run_capability_boundary_acceptance.py \
  --output reports/evaluation-runs/capability-boundary-$(date +%Y%m%d-%H%M%S)
```

---

## 8. 明确未做 / 建议下一步

| 优先级 | 事项 |
|---|---|
| **P0 待拍板** | 前端新建任务切到语义入口（`App.tsx` 一行；D15 并排数据已具备，见 §23.1） |
| **P0 建议下一步** | §18.3 A–I 九类红队 runner 接语义入口（本轮只接了 D1/D2 主集，见 §23.8） |
| P0 | §18.3 J：把直播坏案例脱敏后进 D6（目前 D6 仍为 0）；或 D4 24 题计费复测（需 `--confirm-billable`） |
| P1 | Judge 真实计费跑一次 + 找第二个标注者做人工双评（现为单标注者单轮，见 §23.5） |
| P1 | `recover_interrupted_tasks()` 仍 `list_tasks()` 全表；10 万行场景启动恢复会反序列化历史行（claim 扫描已走索引） |
| P2 | 改航/补槽 merge 语义分层 + 槽 provenance 全链路（换目的地、改出发地、改单程/酒店/会议/返程已做） |
| ~~P2~~ 已完成 | 前端「我的差旅 / 政策 / 审计」演示数据已清除，改接真 API（见 §23.6） |
| P3 | CLI `--case-id` 单题调试 |
| 已完成 | DeepSeek 24/24、24×3、Duffel 重验、D14、参数环、多轮 LLM、intake 红队、§18.3 A–I、D15 语义入口评测、LLM Judge 链路、前端去演示数据 |
| B | 可选人工抽检意图金标 / 输出质量 / 影子幻觉 |
| D | 系统事件全剧本加深 |
| E | 02/04–07 盲填大表：**默认不做** |

---

## 9. 新 session 建议开场白（复制即可）

```text
读 HANDOFF.md 续作 corporate-travel-agent，先读 §23（最新一轮），再读 §8 未做清单。
先读 AGENTS.md：说明用简单语言，专业名词先解释再用。
§18.3 A–I 已打完；D15 语义入口评测已 PASS；LLM Judge 已接通但未真跑；前端演示数据已清。
当前工作在分支 semantic-entrypoint-and-judge，工作树干净。
不要再堆缺槽闲聊，不要重复 A–I。下一优先：前端切语义入口（待拍板，一行），
或把 A–I 红队 runner 接语义入口，或 D6 回流。
当前 API 若需启动：set -a && . ./.env && set +a && export DATABASE_URL= && .venv/bin/uvicorn corporate_travel_agent.api.main:app --host 127.0.0.1 --port 8000
（本机 Postgres 未开时必须空掉 DATABASE_URL，否则起不来。AUTH 直播验收用独立 8010 mock，不要复用 8000 上的 Duffel 红队进程。）
```

---

## 10. 风险与注意

- `build_agent_eval_v1.py` **无 --check** 会重写 cases/worlds/manifest；改 SPECS 后必须重建并更新 subset 的 `source_*_sha256`。  
- 改 `intent_calibration` / prompt 后，**旧 smoke 数字不可直接对比**，应新开报告目录。  
- **output 目录勿预先 mkdir**：runner 要求目录不存在。  
- D14 会创建外部 Test Order；每次运行都需要新的明确授权，必须使用 `duffel_test_` token，且取消门禁不可省略。  
- live 依赖 MockProvider 扩展（fail_once / valid_until / handoff 过期）。  
- 政策：`PolicyEngine` 按服务日期判有效期；未知职级 → `INSUFFICIENT_EVIDENCE`。  
- 计费必须 `--confirm-billable` + 配置价格表含目标 model。  
- 当前熔断器和调度线程是**进程内**实现；SQL 仓储可恢复等待任务并用乐观锁避免陈旧更新，
  但多实例部署仍应改为 Redis/数据库共享熔断状态 + 正式任务队列/Outbox。  
- `WAITING_FOR_PROVIDER` 的重启恢复要求启用 SQL 仓储；默认内存仓储重启后不会保留任务。  
- 即时/延迟重试继续消耗原任务 12 次工具预算，因此可能在第 3 次延迟尝试前进入
  `TOOL_BUDGET_EXHAUSTED`；订单、支付、取消等外部写操作禁止盲目自动重试。  

---

## 11. 参考项目：参数提取标准环（本会话学习笔记）

对照 Downloads 下 **`grok-build`** 与 **`Anthropic-Leaked-Source-Code`**，与本仓库 P0/P1 对齐。

### 共同标准环（两项目一致）

```text
模型产出 tool_use / 参数 JSON
  → L1 模式校验（Zod safeParse / serde + JSON Schema）
  → L2 业务校验（validateInput / ResourceType::validate_params_value）
  → 失败：结构化错误写回 tool_result（is_error），进入外层 agent 下一轮
  → 成功：执行副作用
```

**核心原则：校验失败不静默编造缺参；把可修复错误交给模型下一跳。**

### Anthropic（Claude Code）要点

| 机制 | 位置 | 作用 |
|---|---|---|
| Zod `inputSchema.safeParse` | `services/tools/toolExecution.ts` | 类型/必填；注释写明 model 经常给错类型 |
| `formatZodValidationError` | `utils/toolErrors.ts` | 人读错误：缺参 / 多余键 / 类型不匹配 |
| `tool.validateInput` | 各 Tool（如 `FileEditTool`） | 业务：路径、文件存在、old≠new、权限 deny 等 |
| 错误回灌 | `tool_result` + `is_error` + `<tool_use_error>` | 模型在 agent loop 里改参重试 |
| deferred schema hint | `buildSchemaNotSentHint` | 未 load 的工具 schema 未进 prompt → 字符串化类型错误，提示先 ToolSearch |

### grok-build 要点

| 机制 | 位置 | 作用 |
|---|---|---|
| JSON Schema → `ToolArgument` | `xai-tool-types/schema_utils.rs` | 把 schema 扁平化给模型（required/enum/bounds） |
| `validate_params_json<T>` | `params_validation.rs` | L1 serde 反序列化 + 分类错误（missing/unknown/type） |
| `ResourceType::validate_params_value` | 各 tool（如 Bash） | L2 交叉约束（例：auto_bg 依赖 enabled_background） |
| `ParamValidationError` | field_path / expected / bad_value / category | 可定位、可回灌的错误对象 |
| 宽松反序列化 | `schema.rs` lenient u64/bool | 容忍 `"123"` 数字串等常见模型笔误，**不是**允许乱编语义 |
| Workflow schema_retry | workflow runtime | schema 重试**不**计入 agent_budget |

### 本仓库（CTA）映射

| 参考层 | CTA 对应 |
|---|---|
| L1 schema | `IntentExtractionSchema` Structured Outputs + `extra=forbid` |
| 防编造 allowlist | `provided_fields` + `merge_model_fields_anti_fabrication`（比两参考更严：未声明字段直接丢弃） |
| L2 业务 | `search_ready_missing` + `_validate_intent_fields` |
| 确定性补全（参考项目一般不做） | **P0** 白名单派生（仅从已有槽推时间/酒店对；**永不编城市**） |
| 定向 repair（≈ tool_result 重试） | **P1** ≤2 次，只补日期/酒店；城市只澄清 |
| 外层预算 | 澄清轮次 + tool budget |

### 已本地化（Claude Code 参数环）

| Claude Code | 本仓库 |
|---|---|
| `inputSchema.safeParse` | `validate_schema_l1` |
| `tool.validateInput` | `validate_business_l2` |
| `formatZodValidationError` | `format_tool_use_error` |
| `tool_result is_error` 回灌 | `build_tool_use_error_repair_payload` + `<tool_use_error>` system 段 |
| agent 下一跳改参 | `LoopAction.REPAIR` ≤2（仅日期/酒店） |
| 不可修则停 | `LoopAction.CLARIFY`（城市/冲突）或 OUT_OF_SCOPE |

审计：`task.metadata["param_loop"]` / `intent_calibration.param_loop`（`pattern=claude_code_param_loop_v1`）。

### 可继续借鉴（未做 / 半做）

1. ~~更结构化的校验失败文案~~ → **已做**  
2. **单题 CLI** `--case-id`  
3. **provenance 全链路**（半套 field_provenance）  
4. ~~参数环 + DeepSeek 复测~~ → smoke 24/24、稳定性 24×3  
5. **失败可续**（类 Claude 补 tool_result 还能接着跑）→ §13 Step1 **已做**（structured 再聊）；Step2 澄清优先未做  
6. 修订语义分层（补缺 / 改时间 / 换航线 / 对抗）  
7. **错误副作用分类** → `agent/error_recovery.py`（`SideEffectClass` / `RecoveryAction`）；tool/trace 已挂字段  

---

## 12. 多轮真实 LLM：指标怎么算

**不是「每轮打分再平均」，也不是「只评最后一轮」。**

| 类型 | 算法 |
|---|---|
| **题是否通过** | 整题 hard assertions **全部**过，且无 runner error |
| **final_state / 政策 / 预订** | 看**整题结束后**的 task |
| **工具序 / 禁工具** | 看**全程** `tools_called`（去重保序；**任一轮**调了禁工具就挂） |
| **real_model_calls / token / 成本** | **每次** extract **累加**（24 题可有 36～39 次 LLM） |

含义：第一轮误搜酒店，即使第二轮改对航线，forbidden 仍可能挂。

---

## 13. 改进清单 +「中间失败卡死」方案

### 13.1 可改进总表

| # | 问题 | 现在 | 建议 |
|---|---|---|---|
| 1 | **中间失败后后续句不处理** | **Step1 已做**：structured 可再聊 + 评测白名单 | OOS 仍终态；见 Step3 |
| 2 | 改航/补信息/乱说话同一 merge | 粘旧参数或误清空 | 分：补缺 / 改时间 / 换航线 |
| 3 | 第二轮误 OOS | 整题废 | 有有效槽时勿 OOS；允许新意图重开 |
| 4 | 中间禁工具污染终态分 | 全程累计 | 业务少误搜；或评测分阶段 |
| 5 | 字段来源不清 | provenance 半套 | 每槽记来源+轮次 |
| 6 | 多轮 LLM 无单独预算 | 仅 tool 总限 | `max_llm_turns` |
| 7 | 失败不能填表续跑 | 表单路径仍在；对话再聊已开 | 与 form 并存 |
| 8 | 副作用/恢复动作无统一词汇 | **已做** `error_recovery.py` | 未来 Order 接 `EXTERNAL_WRITE_*` |

### 13.2 卡死问题：原因与解法

**现象（修复前）**

```text
skipped user turn in state=NEEDS_STRUCTURED_INPUT (not open for sequential LLM)
skipped ... in state=OUT_OF_SCOPE
```

**原因（两层）**

1. **编排**：LLM 失败 → `NEEDS_STRUCTURED_INPUT`；误 OOS → `OUT_OF_SCOPE`（无出口）。  
2. **评测白名单** `_SEQUENTIAL_LLM_STATES` 曾不含 structured → 后面用户句全部 skip。

**落地进度**

| 步 | 改什么 | 状态 |
|---|---|---|
| **1（小）** | `submit_message` 允许 `NEEDS_STRUCTURED_INPUT`；评测白名单加上 | **已做**（2026-08-09） |
| **2（中）** | LLM 失败：重试后优先 **澄清**，少直接 structured | **代码已有**（`test_language_model_failure_prefers_clarification` + 429 重试后成功）；不要当新洞重做 |
| **3（中）** | OOS 收紧 + 允许用户新意图回 `DRAFT` | 未做 |
| **4** | 真死局（tool budget 耗尽）才 skip | 部分：budget 仍拒绝 submit |

**错误恢复 taxonomy（已做）**

- 模块：`src/corporate_travel_agent/agent/error_recovery.py`
- 字段：`side_effect_class` / `recovery_action`（tool record + WorkflowTraceEvent + TraceStep）
- 多腿 recon：`task.metadata["search_failure_recon"]`（成功腿 / 失败腿 / partial）
- 单测：`tests/test_error_recovery.py`

**验收（Step1）**

- 第 1 次 extract 失败，第 2 句完整出差话 → `submit_message` 再抽  
- 评测不再因 `NEEDS_STRUCTURED_INPUT` 无条件 skip  
- mock `LanguageModelError` 后仍能 `submit_message`（单测覆盖）

### 13.3 对照 Anthropic（人话）

| Claude Code | 我们 |
|---|---|
| 参数错了退回模型改 | 有 L1/L2 + repair |
| 一步失败仍补结果，对话能续 | **Step1**：structured 可再聊；OOS 仍弱 |
| withhold → recovery continue | taxonomy + provider 有界重试；写操作走 recon |
| maxTurns / 预算 | 有 tool 限；多轮 LLM 预算可加 |

---

## 14. DeepSeek 适配注意

- 默认模型：`OPENAI_MODEL=deepseek-v4-pro`  
- `OPENAI_BASE_URL=https://api.deepseek.com`  
- **pro 不支持 Responses API** → 适配器自动走 **Chat Completions + json_object + thinking:disabled**  
- flash 仍可走 Responses  
- `.env` 里 API key **不要前导空格**（否则 `source` 失败）  
- 价格表已含 `deepseek-v4-pro`：$0.435 in / $0.87 out  

---

## 15. 2026-08-10 Provider 稳定性与恢复机制

### 15.1 已完成的真实调用证据

完整只读链路为 Duffel 航班搜索 + LiteAPI 酒店搜索 + 组合方案 + 选择 + 两侧重验/交接。
稳定性脚本不再把 `max_provider_attempts` 强制为 1，系统默认值统一为 3；这里的 3 是
**总尝试数（首次 + 最多 2 次即时重试）**，不是首次之外再重试 3 次。

| 运行 | 结果 | 关键证据 | 报告目录 |
|---|---|---|---|
| 完整链路 3 次 | **3/3 最终通过** | 三轮均无重试 | `reports/evaluation-runs/duffel-liteapi-full-chain-stability-v2-20260809-1/` |
| 完整链路 10 次 | **4/10 最终通过；3/10 无重试通过** | attempt 1–6 的 Duffel 连接错误均耗尽 3 次；attempt 7 首次失败后即时重试恢复；attempt 8–10 无故障 | `reports/evaluation-runs/duffel-liteapi-full-chain-stability-v2-20260810-10x-1/` |

10 次运行显示的失败簇约持续 101 秒。结论不是“系统稳定通过”，而是：即时重试确实能恢复
短瞬断，但无法覆盖分钟级上游故障，因此新增熔断和任务级延迟重试。

### 15.2 当前实现

| 能力 | 当前行为 | 位置 |
|---|---|---|
| 即时尝试 | 只读 Provider 每次调用默认最多 3 次；指数退避、原因码、`retry_of` 和工具预算照常记录 | `agent/orchestrator.py` |
| 熔断器 | 即时尝试耗尽后打开 60 秒；期间新任务不打 Provider；到期只放一个 half-open 探针 | `services/provider_resilience.py` |
| 新任务状态 | 可恢复错误进入 `WAITING_FOR_PROVIDER`；不可重试错误仍直接进入 `PROVIDER_FAILED` | `domain/enums.py`, `workflow/state_machine.py` |
| 延迟任务重试 | 默认 60 秒 / 180 秒 / 600 秒，最多 3 次；恢复后继续 `SEARCH` 或 `REVALIDATE` | `agent/orchestrator.py` |
| 后台调度 | FastAPI lifespan 启动 daemon worker，默认每 5 秒扫描到期任务，不阻塞原请求 | `services/provider_resilience.py`, `api/main.py` |
| 持久化恢复 | 次数、下一执行时间、恢复操作和熔断截止写入 `TripTask.metadata["provider_retry"]`；SQL 仓储重启后继续 | `agent/orchestrator.py`, `services/sqlalchemy_repository.py` |
| 安全边界 | 仅 `RetryableProviderError` 且无外部副作用的读/读式调用进入自动链路；Order/Payment/Cancel 不自动重试 | `agent/error_recovery.py`, `agent/orchestrator.py` |
| API 可见性 | 任务响应新增 `provider_retry`；`GET /health` 新增 `provider_resilience` | `api/main.py` |

默认环境变量已写入 `.env.example`：

```bash
PROVIDER_CIRCUIT_OPEN_SECONDS=60
MAX_DELAYED_PROVIDER_RETRIES=3
PROVIDER_DELAYED_RETRY_SECONDS=60,180,600
PROVIDER_RETRY_POLL_SECONDS=5
```

### 15.3 状态和返回语义

```text
只读调用瞬时失败
  → 最多 3 次即时尝试
  → 仍失败：WAITING_FOR_PROVIDER + provider_retry.next_retry_at
  → 延迟任务恢复：继续搜索/重验，最终进入正常业务状态
  → 3 次延迟任务仍失败：PROVIDER_FAILED
  → 过程中工具预算耗尽：TOOL_BUDGET_EXHAUSTED
```

- `WAITING_FOR_PROVIDER` 不是成功，也不是空库存；API 会立即返回当前任务和下一重试时间。
- 手动 `/replan` 在等待态不会绕过仍然打开的熔断器；延迟次数耗尽后，显式手动 replan 会
  开启新的重试周期，但仍沿用原任务工具预算。
- 成功恢复会保留 `provider_retry.status=recovered` 和已完成次数，便于评测与审计。
- 最终失败不会创建 Booking Intent；已有幂等键逻辑不变。

### 15.4 验证状态

已覆盖：

- 即时尝试耗尽后进入 `WAITING_FOR_PROVIDER`；
- 第一次延迟尝试恢复；
- 3 次延迟尝试耗尽进入 `PROVIDER_FAILED`；
- 熔断期间新任务不调用 Provider；
- 重验阶段恢复且不重复创建 Booking Intent；
- SQLite/SQLAlchemy 仓储关闭重开后继续到期任务；
- API 健康检查和评测状态映射识别新状态；
- FastAPI lifespan 能启动并停止后台调度线程。

验证命令与结果：

```bash
.venv/bin/pytest -q
# 全量通过；仅有现存 Starlette/httpx TestClient 弃用警告

.venv/bin/ruff check <本次修改的 Python 文件>
.venv/bin/python -m compileall -q src/corporate_travel_agent
# 通过
```

核心回归位于 `tests/test_provider_retry_and_disclosure.py`、`tests/test_persistence.py` 和
`tests/test_api.py`。

### 15.5 尚未完成 / 接手注意

1. 新增的 60 秒熔断 + 延迟任务级重试尚未专门做一次**真实上游故障恢复评测**；之前的真实
   10 次运行只覆盖了即时重试。下一轮若进行外部调用，仍需明确授权并使用新报告目录。
2. 当前熔断状态是进程内状态；启动时可从等待任务恢复 `open_until`，但多实例之间不共享。
   生产环境建议 Redis/数据库共享熔断器，并用正式任务队列/Outbox 取代 daemon thread。
3. 只有 SQL 仓储能跨进程重启保存等待任务；默认内存仓储只适合开发和单测。
4. 延迟重试与即时尝试共用任务的 12 次工具预算，所以“最多 3 次”是上限，不是保证一定执行
   3 次；预算不足会更早安全停止。
5. 真实稳定性脚本仍是一次性 runner，不会在 CLI 进程内等待 10 分钟跑完全部延迟计划；若要
   评估延迟机制，应运行 API 服务与后台调度器，或提供专用受控评测入口。

---

## 16. 2026-08-19 下一 Session：后端优先项 1、2

> 状态：**16.1 已实施；16.2 代码完成，PostgreSQL 双 worker / lease 接管 / 10 万行 EXPLAIN 已留证（§19 F）。启动恢复仍全表 `list_tasks()`。**
> 最近一次已知全量基线为 Python 455 tests passed、Ruff 通过、前端 lint / 7 tests / build
> 通过；开始修改前先在当前分支复跑并记录结果。

### 16.1 P0 — Provider 重验上下文必须持久化（已完成）

**问题与已知复现**

- Duffel 和 LiteAPI 的搜索结果上下文只存在各自实例的 `_offer_cache`：
  `providers/duffel.py`、`providers/liteapi.py`。
- Worker A 完成搜索后，如果选择请求落到 Worker B，或服务在等待用户选择期间重启，新实例
  无法取得供应商重验所需上下文，会直接把原本有效的选项判为 `UNAVAILABLE`，甚至不会向
  Provider 发起 HTTP 请求。
- 该问题已经用两个 Duffel 实例复现：第二个实例返回 `UNAVAILABLE`，Provider HTTP 调用数为 0。
  证据与原始审计说明见 `reports/_audit-wip/backend.md`。
- 当前持久化测试主要通过 Mock Provider 验证任务恢复，未覆盖真实 Provider adapter 的跨实例
  重验，因此会掩盖此问题。
- `_offer_cache` 与 `revalidation_raw_responses` 目前也没有明确的容量/TTL 边界，长生命周期进程
  存在持续增长风险。

**目标方案**

1. 定义独立端口 `ProviderQuoteContextStore`，提供内存实现（单测/开发）与 SQL 持久化实现。
2. 以 `provider + task/snapshot + ref_id` 为稳定键，保存完成重验所需的最小上下文、价格、币种、
   供应商标识和过期时间；不得保存 API key、认证头等凭据。
3. Duffel 至少保存 offer reference、金额/币种和有效期；LiteAPI 至少保存 hotel/query context、
   raw offer/room signature、金额/币种和有效期。字段以 adapter 实际重验输入为准。
4. 搜索结果和对应 quote context 必须在返回给调用方前持久化；优先与 inventory snapshot 放入
   同一工作单元，避免“snapshot 已提交、context 丢失”的半状态。
5. `revalidate()` / deeplink 路径从持久化 store 重建输入，进程内缓存只能作为可丢弃的性能层，
   不能要求 sticky session 或 worker affinity。
6. 增加 TTL/过期清理与明确的缺失/过期错误码；失败必须 fail closed，且能区分“供应商无库存”
   和“本地重验上下文缺失”。

**验收门槛**

- Worker A 搜索并持久化；销毁实例；Worker B 使用同一数据库选择/重验，Duffel 与 LiteAPI 均会
  实际进入预期的 Provider 重验路径，而不是因缓存为空返回 `UNAVAILABLE`。
- 服务在 `WAITING_FOR_USER` 等待态重启后，原任务仍可继续选择、重验并进入正常后续状态。
- 缺失和过期 context 有稳定错误码、审计事件和用户可操作的恢复路径。
- 并发实例读取一致；context 不包含凭据；缓存/审计辅助列表有 TTL 或容量上限。
- 新增 adapter 级、SQL 持久化、进程重启和多 worker 集成测试；PostgreSQL 路径为生产验收基准。

### 16.2 P1 — 延迟重试需要真正的分布式 claim / lease（代码已完成）

**问题与根因**

- SQL `list_due_provider_retries()` 虽然在 PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`，但返回任务
  后查询事务立即结束；真正执行 Provider 调用时已没有锁，也没有持久化 claim。
- 多个 API worker 的 lifespan 都会启动自己的 `ProviderRetryScheduler`，因此同一个到期任务可能
  被多个进程同时取走并调用 Provider。最终的 optimistic revision 只能阻止部分重复写入，不能
  撤销已经发生的重复外部调用。
- 调度器的进程内 `_delayed_retry_lock` 只对单进程有效；当前任务没有 `lease_owner`、
  `lease_until`、`attempt_token` 等所有权字段。
- SQL 到期查询已有过滤，但内存 fallback 和部分恢复路径仍会调用 `list_tasks()`；启动/恢复与
  公共列表在数据量大时仍有全表加载风险。
- `ProviderRetryScheduler.stop()` 最多等待 5 秒；若 Provider 调用仍在执行，关闭流程可能继续，
  需要明确 drain/超时后的资源所有权，避免活跃任务使用已关闭的 HTTP/DB client。

**目标方案**

1. 将延迟重试建模为可 claim 的持久化 job（优先独立 job/outbox 表；若暂留 task 表，则新增
   `lease_owner`、`lease_until`、`attempt_token` 和必要的状态/时间索引）。
2. 实现原子的 `claim_due_retries(worker_id, now, lease_duration, limit)`：在同一事务内选择到期且
   lease 已过期的行、写入 lease/唯一 attempt token，并返回已 claim 的任务；PostgreSQL 可使用
   `FOR UPDATE SKIP LOCKED` + `UPDATE ... RETURNING`。
3. Provider 调用完成后的写回必须校验 attempt/fencing token；旧 lease 的迟到结果不得覆盖新
   worker 的结果。耗时调用需要 heartbeat/续租或足够保守且有上限的 lease。
4. 将 retry scheduler 迁到专用 worker 角色；Web API worker 默认不启动后台消费者，并通过配置
   明确区分 `api` / `worker` 进程。
5. stale in-progress 恢复按 `state + updated_at/lease_until` 的索引查询执行，不遍历全部任务；
   公共任务列表继续保持 actor scope、limit/cursor。
6. 关闭流程先停止 claim、等待在途工作 drain，再关闭 Provider/数据库资源；超时后保留 lease
   让其他 worker 在过期后安全接管。

**验收门槛**

- 两个独立 worker 同时竞争一个到期 job：只产生一次 Provider 调用和一次有效状态提交。
- claimant 在调用前/调用中崩溃：lease 过期后另一 worker 可接管；旧 attempt 的迟到写回被拒绝。
- 10 万历史任务场景的到期扫描使用索引，不反序列化/加载全表；用 PostgreSQL `EXPLAIN` 留证。
- 多 worker、进程重启、lease expiry、heartbeat、优雅关闭均有集成测试；SQLite 可做快速测试，
  但 `SKIP LOCKED` 与并发 claim 必须以 PostgreSQL 集成测试为权威。
- 增加可观测指标：claimed、lease_conflict、reclaimed、completed、exhausted、oldest_due_lag。

### 16.3 建议实施顺序与保护边界

1. 16.1 已完成跨实例失败特征测试、store、migration、应用装配和 adapter 改造。
2. 16.2 已落地 schema、atomic claim、lease expiry reclaim、fencing 和专用 worker 角色。
3. PostgreSQL 16 双 worker 并发、lease 过期接管、旧 token 写回拒绝、10 万行 `EXPLAIN` 已留证（§19 F）。
4. 计费模型 / 真实 Provider **写操作**仍需明确授权；V1 不做 Booking/Payment/Cancel 外部写入。

**下一 session 建议开场白：** 用 §9，不要用这段旧文案。

---

## 17. 2026-08-19 本 Session 实施结果

### 17.1 ProviderQuoteContextStore

- 新增 `services/provider_quote_context.py`，定义小接口及有容量上限的内存 adapter；SQL adapter
  位于 `services/sqlalchemy_repository.py`。
- 迁移 `0005_provider_quote_contexts` 保存 `provider + snapshot_id + ref_id`、价格、币种、最小
  重验 payload 和 `expires_at`；凭证键在写入前拒绝。
- Duffel/LiteAPI 搜索在返回快照前持久化上下文；新实例从 store 重建请求，仍实际调用 Provider
  重验。缺失/过期/损坏分别使用稳定错误码并 fail closed；过期记录支持 TTL 清理。
- `revalidation_raw_responses` 改为容量 100 的有界 deque。

### 17.2 Provider retry claim / lease / fencing

- 迁移 `0006_provider_retry_leases` 为任务增加 `retry_lease_owner`、`retry_lease_until`、
  `retry_attempt_token` 及状态/lease 索引。
- SQL claim 在同一事务中选择并写 lease；PostgreSQL 路径使用 `FOR UPDATE SKIP LOCKED`。
- lease 到期可接管 `WAITING_FOR_PROVIDER` 或 stale `SEARCHING/REVALIDATING`；仓储写回校验 fencing
  token，旧 worker 迟到结果被拒绝。
- `PROCESS_ROLE=api` 默认不启动消费者；`worker/all` 才启动。默认 lease 900 秒。关闭先停止
  claim 并 drain，在途未结束时不提前关闭 Provider/DB 资源。
- 健康检查新增 claimed、lease_conflict、reclaimed、completed、exhausted、oldest_due_lag 指标。

### 17.3 验证与未完成项

- Python 全量 **465 tests passed**；Ruff、compileall、前端 lint、7 tests 和 production build 通过。
- Duffel/LiteAPI 内存跨实例与 SQLite 关闭/重开重验通过；第二实例均实际进入 Provider HTTP
  路径。
- SQL lease 测试覆盖单 owner、有效 lease 阻止二次 claim、过期接管、旧 token 写回拒绝。
- 未发起任何真实 Provider 或计费模型调用。
- PostgreSQL 16 双进程并发 claim、lease 过期接管、旧 fencing token 写回拒绝、10 万行
  `EXPLAIN`（`Bitmap Heap Scan`，非 Seq Scan）已留证：`reports/evaluation-runs/postgres-dual-worker-20260819/`。
  启动恢复 `recover_interrupted_tasks()` 仍会 `list_tasks()` 全表，不算完全生产验收。

---

---

## 18. 2026-08-19 后半 Session：意图红队与未测类型

### 18.1 本轮做了什么

对运行中的 FastAPI（`deepseek-v4-pro` + Duffel Test + LiteAPI sandbox，`AUTH_ENABLED=false`，内存仓储）做了自然语言红队，并修了打出来的洞。

| 轮 | 产物 | 结果 |
|---|---|---|
| Round 1 | `reports/evaluation-runs/redteam-longtail-20260819/` | 63 条；断言 60/63，复盘另有产品错（尺子偏松） |
| Round 2 | `reports/evaluation-runs/redteam-round2-20260819/` | 12 条严断言；intake 类多数过，工作流/审批未打穿 |
| Runner | `run_redteam.py`、`run_redteam_round2.py` | 可 `--case-id` 过滤 |

**已修并直播复测通过：**

- 近过去月日跳年（`8月5日` 不再收成 2027）
- 条件酒店（`如果回不来再订酒店` → `UNSPECIFIED`）
- 澄清中闲聊不再整单 `OUT_OF_SCOPE`
- 澄清预算耗尽但核心槽已齐 → 搜索而不是卡死 structured
- 日料/天气 side-request 降为非阻塞冲突
- `北京或者上海` 不再编成航线
- 「对比高铁和飞机」清掉残留 `flight_only` / `train_only`
- `那去伦敦吧`（无「改为」）清掉旧到达/出发并重锚

相关代码：`intent_calibration.py`、`local_intent.py`、`param_extraction_loop.py`、`orchestrator.py`、`openai_adapter.py`。  
回归：`tests/test_local_intent.py`、`test_intent_calibration.py`、`test_intent_scenarios.py`、`test_param_extraction_loop.py`。

### 18.2 已经测过的类型（不要重复堆）

- 单槽/多槽缺失、空话「你看着办」
- 中英夹杂、错别字、帝都/魔都、注入、改身份、口头批准
- 时区（美东、北京→旧金山跨日）
- 会议 11 点提前一小时不减缓冲
- 明确不住酒店、深圳不映射上海
- 澄清时「选第一名」、演唱会后重开差旅
- 当天往返+住一晚必须问、多城/家属/软卧披露、浦东不当城市
- 前端按钮等价 token：`return:same_day_afternoon` → 13:00–18:00（API 层）；澄清面板点选已直播（§19 D-04）
- 认证越权 404：pytest + **AUTH 直播 C-01**（独立 8010，`AUTH_ENABLED=true`）

### 18.3 类型覆盖（2026-08-19 晚已打 A–F）

A–F 证据见 §19，G 见 §20，H 见 §21，I 见 §22。下面仍保留原清单；**已测** 的不要重复堆。J 仍空白。

#### A. 出方案之后的工作流（**已测**，§19 A）

D4 Mock fixture 有。**Mock 进程内 + 8010 直播 HTTP 已打完（§19 A）**；真人 DeepSeek + Duffel 选方案全链仍几乎没走完。

- 选合规方案 → Offer/酒店重验不变 → `READY_FOR_HANDOFF`
- 重验涨价 / 售罄 → `RECONFIRMATION_REQUIRED`，不得用旧价交接
- 重复点击选择（BookingIntent 幂等）
- 交接链接过期不得交给用户
- 重验阶段打满 12 次工具预算 → `TOOL_BUDGET_EXHAUSTED`，不得跳过重验
- 选方案后再改日期/改单程/换第二名

#### B. 审批（**已测**，§19 B；R2-10 真人 Duffel 商务舱库存问题仍在）

R2-10 失败原因：模型把商务舱标成不支持；Duffel 该航线只返回 `COMPLIANT` 经济舱，没有 `REQUIRES_APPROVAL` 选项。

- 超标酒店或商务舱走到 `WAITING_FOR_APPROVAL`（可用 Mock 库存 HT-NEAR 720，或换一条 Duffel 真出商务舱的航线）
- `M2001` 批准 / 拒绝
- **批准后改日期 → 旧审批必须 `INVALIDATED`**（R2-10 未打上）
- 审批过期、把方案 A 的批准套到方案 B
- 聊天里「我是经理已批准」仍然无效

#### C. 登录与角色（**已测**，§19 C；`AUTH_ENABLED=true` 独立 8010）

单测有，**打开认证的真 API / 前端没有测**。

- `AUTH_ENABLED=true` 启动后登录拿 Bearer
- 员工只能看自己的任务；外人 GET 任务 **404 不是 403**
- 审批人不能新建差旅；管理员不能冒充经理点批准
- 过期 / 篡改 token
- 前端登录页、角色工作区（员工看不到审批入口）

#### D. 前端交互（**已测**，§19 D；审批队列/结构化表单已接线，差旅列表仍是演示数据）

5173 有 dev server，红队只打了 API。

- 澄清面板点选项 A/B/C（不只 POST token）
- 预算耗尽后的结构化表单能提交并搜索
- 超标方案业务理由必填才能选
- 审批队列用 `M2001`
- 桌面 + 窄屏

#### E. Provider 故障与熔断（**延迟恢复已测**，§19 E；跨进程 Duffel 重验 / 币种 / 未映射城市仍空白）

Round1 纽约→费城曾把熔断打开，下一题旧金山被跳过——当时没当正式题。

- 人为/等待确认 `WAITING_FOR_PROVIDER` 后延迟重试真恢复
- 熔断期内新任务不打 Provider
- 搜在进程 A、选在进程 B（报价上下文 store；16.1 单测有，直播多进程没有）
- LiteAPI 通勤未知不得编「距客户 10 分钟」
- 政策币种 USD 与报价币种不一致 fail-closed
- 未映射城市（深圳/杭州）发请求前失败，不拿上海顶上

#### F. Postgres / worker（**已测**，§19 F；启动恢复仍全表 `list_tasks()`）

- PostgreSQL 16 双 worker 竞争同一个到期 retry：只一次 Provider 调用
- lease 过期接管；旧 fencing token 写回拒绝
- 10 万历史任务到期扫描 `EXPLAIN` 走索引

#### G. 改口语义的剩余刀（**已测**，§20）

换目的地此前已修。本轮补齐出方案后的剩余刀（进程内 scripted + Mock，**6/6**）：

- 已出方案后：`日期不动，改成单程` → 保留去程窗，返程槽 `null`，不再搜 inbound
- `酒店不要了` → `lodging_requirement=NOT_REQUIRED`，酒店日清空，不再搜酒店
- `还是订两晚` → `REQUIRED`，入住=到达日，退房=+2 晚
- `会议改到下午 3 点` → 保留到达日，钟点改为 15:00（目的地 TZ）
- `返程改到后天晚上` → 不去改出程；若时钟相对日早于到达日，则按到达日 +2，18:00–23:00
- 无「改为」的改出发地：`改从上海走` → 只改 origin，保留 destination，不走 destination_revision

#### H. 日历边角（**已测**，§21）

跳年此前已修。本轮补齐（进程内 scripted + Mock，**8/8**）：

- `下下周三`（2026-08-19 时钟）→ 2026-09-02
- `这周五还是下周五` → 日期槽 `null`，澄清要公历，不搜
- `8/5` / `8.5` 在日期之前 → 2026-08-05；时钟已过则年less 仍不跳年
- `2026.8.5` 显式年份 → 即使已过也保留 2026-08-05
- `春节从北京去上海` → 不编公历日，澄清；若同时写了 `1月28日` 则保留
- `12月30日去 1月2日回` → 出程 2026-12-30，返程 2027-01-02

#### I. 能力边界披露（**已测**，§22）

V1 不做。模型判断是否在要这些能力，宿主强制披露且不搜（scripted + Mock，**8/8**）：

- 儿童/婴儿：`带2岁小孩` → 说不支持，不搜
- 签证/护照：说不支持，不把办证当成已安排
- 选座/里程卡：说不含指定座位或积分兑换，不搜
- 开口/缺口/多城：`从杭州回` / `再去杭州` 不收成单段往返
- 已出票改签/退票：不用新库存代替退改
- 无障碍/宠物随行：不按普通成人票假装已满足
- 对照：普通「下周三北京上海开会」仍搜索
- 模型可判定「签证中心开会」不是办签证（I-07）；正则不再覆盖成功抽取的空列表

#### J. 评测资产（P2）

- D6 `bad-case-regression` 仍为 0：本轮直播坏案例未脱敏回流
- 用户可见答复的影子幻觉（无证据的价格/航班号）未评
- 本轮修复后 **未重跑** D4 24 题真实模型 smoke（计费，需 `--confirm-billable`）

### 18.4 建议下一 session 怎么开

1. 读 §18.3 **J**，不要从缺槽开始，也不要重复 A–I。  
2. 启动 API 必须 `export DATABASE_URL=`（除非 Postgres 已 `docker compose up`）。8000 若仍是 Duffel 红队进程，AUTH/前端验收用 `examples/run_uncovered_types_acceptance.py` 的 8010 mock。  
3. 直播断言要严：禁止填的槽必须是 `null`、禁止搜、审批状态、工具名。  
4. 计费模型 / 真实 Provider 写操作仍需用户再次明确授权。D14 式 Test Order 不得默默重跑。  
5. 打出的坏案例先写 `evals/intake/` 或 round 报告，确认后再进 D6。

### 18.5 环境备忘

```bash
set -a && . ./.env && set +a
export DATABASE_URL=          # 本机无 Postgres 时必加
.venv/bin/uvicorn corporate_travel_agent.api.main:app --host 127.0.0.1 --port 8000
# 前端：cd frontend && npm run dev   → 5173 反代 /api 到 8000
```

当前已知：`OPENAI_MODEL=deepseek-v4-pro`，`TRAVEL_PROVIDER=duffel_liteapi`，`AUTH_ENABLED=false`，`LIVE_BOOKING_ENABLED=false`。

---

## 19. 2026-08-19 晚：§18.3 A–F 直播验收

**不要再补缺槽闲聊。** 本轮按 §18.3 打了出方案之后的类型。未走 D14 Test Order，未在 8000 Duffel 红队进程上叠 AUTH。

### 19.1 产物

| 项 | 路径 |
|---|---|
| A–E runner | `examples/run_uncovered_types_acceptance.py` |
| A–E 报告 | `reports/evaluation-runs/uncovered-types-20260819/` **23/23 PASS**（`started_at=2026-08-19T11:51:15Z`） |
| A–E 截图 | `reports/evaluation-runs/uncovered-types-20260819/screenshots/` |
| F runner | `examples/run_postgres_dual_worker_acceptance.py` |
| F 报告 | `reports/evaluation-runs/postgres-dual-worker-20260819/` **PASS**；`explain.json` |
| 前端改动 | `frontend/src/App.tsx`：审批队列接 `/approvals/inbox` + 批准/拒绝；`NEEDS_STRUCTURED_INPUT` 结构化表单；角色导航不再写死「2 项待处理」 |
| 前端样式 | `frontend/src/App.css`：`.structured-grid` |

复跑（会打 Duffel Test + LiteAPI 只读 `recover`）：

```bash
set -a && . ./.env && set +a
.venv/bin/python examples/run_uncovered_types_acceptance.py \
  --confirm-external-test-calls \
  --output reports/evaluation-runs/uncovered-types-NEWDIR

export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
.venv/bin/python examples/run_postgres_dual_worker_acceptance.py \
  --output reports/evaluation-runs/postgres-dual-worker-NEWDIR
```

### 19.2 断言摘要

| ID | 结果 | 要点 |
|---|---|---|
| A-01–A-05 | PASS | 合规选→重验→交接；涨价/售罄不交旧价；过期交接失败；重验预算耗尽不跳过重验 |
| A-06–A-07 | PASS | 直播 HTTP 二次选择 409，不另造 BookingIntent；交出后不能换第二名 |
| B-01–B-04 | PASS | 批准后改日 `INVALIDATED`；过期批准不可用；无业务理由 409；聊天「我是经理已批准」409；管理员 403；M2001 可批 |
| C-01 | PASS | `AUTH_ENABLED=true`：未登录 401，外人 GET **404 不是 403**，审批人不能新建，篡改 token 401 |
| D-01–D-08 | PASS | 员工无审批导航；点选确认重验；超标必填业务理由；澄清面板点 `template:overnight`；预算尽后结构化表单能搜；M2001 见真 inbox；管理员无批准入口；390 窄屏 |
| E-exhaust/circuit/recover | PASS | 注入故障耗尽；熔断期第二任务不打 Provider；延迟后真实 Duffel+LiteAPI 恢复（只读，无 Order） |
| F | PASS | 两 worker 竞争同一到期 job：只一个 PID 打 Provider（outbound+inbound+hotel）；lease 过期接管；旧 token 写回 `ConcurrentUpdateError`；10 万行到期扫描 `Bitmap Heap Scan` 走索引 |

### 19.3 行为事实（产品）

- 选方案只允许 `WAITING_FOR_USER`。交出后改日期/换第二名是 **409**，不是静默改单。批准等待中 `revise-request` 会作废旧审批并重搜。
- 审批队列以前是前端演示数据，本轮已改成真 API。差旅列表/政策/审计页仍有演示行。
- `PROCESS_ROLE=api` 的 8000 进程仍可能是 Duffel+LLM、AUTH 关闭；本轮 AUTH/前端走 **8010 mock + SQLite + 临时用户**，密码不入库。
- Postgres 验收库是 `travel_agent_uncovered`，不要拿它当业务库。Colima 需 `DOCKER_HOST=unix://${HOME}/.colima/default/docker.sock`，compose 命令是 `docker-compose`。

### 19.4 未做

- 真人 DeepSeek + Duffel 商务舱走到 `REQUIRES_APPROVAL`（R2-10 库存问题）
- 跨进程 Duffel 报价上下文直播（16.1 单测已有）
- D6 回流、D4 24 题计费 smoke
- 启动路径 `recover_interrupted_tasks` 仍全表 `list_tasks()`

---

## 20. 2026-08-19：§18.3 G 出方案后改口

**不要再补缺槽闲聊，也不要重复 G。** 本轮只打出方案之后的改口刀。未走 D14，未计费。

### 20.1 产物

| 项 | 路径 |
|---|---|
| G runner | `examples/run_revision_knives_acceptance.py` |
| G 报告 | `reports/evaluation-runs/revision-knives-20260819/` **6/6 PASS**（`started_at=2026-08-19T13:39:08Z`） |
| 校准 | `intent_calibration.apply_followup_slot_revisions`；`当天晚上回` 视为返程证据 |
| 本地解析 | `local_intent`：返程向 后天/明天不写出程；晚上返程 18:00–23:00 |
| 编排 | `orchestrator._maybe_reset_superseded_origin` |
| 单测 | `tests/test_intent_scenarios.py` G 用例；`tests/test_intent_calibration.py`；`tests/test_local_intent.py` |

复跑（output 目录必须不存在）：

```bash
.venv/bin/python examples/run_revision_knives_acceptance.py \
  --output reports/evaluation-runs/revision-knives-NEWDIR
```

### 20.2 断言摘要

| ID | 结果 | 要点 |
|---|---|---|
| G-01 | PASS | `日期不动，改成单程`：去程窗不变，返程 `null`，不新增 inbound 搜索 |
| G-02a | PASS | `酒店不要了`：`NOT_REQUIRED`，酒店日 `null`，不新增酒店搜索，返程保留 |
| G-02b | PASS | `还是订两晚`：入住=到达日 2026-08-06，退房=2026-08-08 |
| G-03a | PASS | `会议改到下午 3 点`：到达日不变，15:00，出发窗不变 |
| G-03b | PASS | `返程改到后天晚上`：时钟 8/1+2 早于到达 8/6，故按到达+2 → 8/8 18:00–23:00 |
| G-04 | PASS | `改从上海走`：origin=Shanghai，destination 仍为 London，无 `destination_revision` |

### 20.3 行为事实

- 出方案后的这些改口走 `submit_message` 的 copy-on-write 重搜，不是 409。交出后改日期/换第二名仍是 409（A 已测）。
- `返程改到后天晚上`：若「今天+2」已经不早于到达日，用时钟相对日；否则用到达日+2，避免把返程改到出发之前。
- `改从上海走` 只改出发地。北京→上海再改从上海走会变成同城，本轮 G-04 用伦敦目的地避开这条退化航线。
- 本轮未改 prompt，旧 DeepSeek 24/24 数字仍可对照；未重跑计费 smoke。

### 20.4 未做

- 真人 DeepSeek 出方案后再改口（本轮 scripted）
- 启动路径 `recover_interrupted_tasks` 仍全表 `list_tasks()`

---

## 21. 2026-08-19：§18.3 H 日历边角

**不要再补缺槽闲聊，也不要重复 H。** 本轮只打日期解析边角。未走 D14，未计费。

### 21.1 产物

| 项 | 路径 |
|---|---|
| H runner | `examples/run_calendar_edge_acceptance.py` |
| H 报告 | `reports/evaluation-runs/calendar-edge-20260819/` **8/8 PASS**（`started_at=2026-08-19T13:57:00Z`） |
| 校准 | `iter_calendar_date_mentions` / `scrub_ambiguous_weekday_choice` / `scrub_ungrounded_holiday_dates` |
| 本地解析 | `下下` 周；斜杠/点分日期；返程月日按出程日进位 |
| 澄清 | `travel_date`：要公历，不用「明天出差」模板 |
| 单测 | `tests/test_local_intent.py`；`tests/test_intent_calibration.py`；`tests/test_intent_scenarios.py` H 用例 |

复跑（output 目录必须不存在）：

```bash
.venv/bin/python examples/run_calendar_edge_acceptance.py \
  --output reports/evaluation-runs/calendar-edge-NEWDIR
```

### 21.2 断言摘要

| ID | 结果 | 要点 |
|---|---|---|
| H-01 | PASS | `下下周三` @ 2026-08-19（周三）→ 2026-09-02 |
| H-02 | PASS | `这周五还是下周五`：模型编 8/21 也被刷掉，澄清要公历 |
| H-03a/b | PASS | `8/5`、`8.5` 在 8/1 时钟 → 2026-08-05 |
| H-03c | PASS | `2026.8.5` 在 8/19 仍保留 2026-08-05 |
| H-03d | PASS | 年less `8/5` 在 8/19 仍不跳到 2027 |
| H-04 | PASS | `春节` 不编 2027-02-17，城市仍解析 |
| H-05 | PASS | `12月30日去 1月2日回` → 2026-12-30 / 2027-01-02 |

### 21.3 行为事实

- 农历/节假日名称不是出行日。同时写了公历（如「春节期间，1月28日」）则用公历。
- 二选一周日与二选一城市同一原则：不猜，问。
- `1月2日回` 现已视为返程证据，不再被 anti-fabrication 当 ungrounded 丢掉。
- 本轮未改 prompt，旧 DeepSeek 24/24 数字仍可对照。

### 21.4 未做

- 农历→公历换算表（刻意不做）
- 真人 DeepSeek 日历边角

---

## 22. 2026-08-19：§18.3 I 能力边界披露

**不要再补缺槽闲聊，也不要重复 I。** 本轮只打 V1 不做的能力。未走 D14，未计费。

### 22.1 产物

| 项 | 路径 |
|---|---|
| I runner | `examples/run_capability_boundary_acceptance.py` |
| I 报告 | `reports/evaluation-runs/capability-boundary-20260819-model/` **8/8 PASS** |
| 判断 | 抽取 schema 字段 `unsupported_capabilities`（模型） |
| 强制 | `orchestrator._apply_capability_boundaries`；`unsupported_capability:` blocking |
| 兜底 | 仅当没有成功抽取时，才对用户原文跑正则 |
| 澄清 | `clarification_questions` 能力边界题，人读「当前不支持…」 |
| 单测 | `tests/test_capability_boundaries.py`；`tests/test_intent_scenarios.py` I 用例 |

复跑（output 目录必须不存在）：

```bash
.venv/bin/python examples/run_capability_boundary_acceptance.py \
  --output reports/evaluation-runs/capability-boundary-NEWDIR
```

### 22.2 断言摘要

| ID | 结果 | 要点 |
|---|---|---|
| I-00 | PASS | 普通差旅仍搜索，无 capability_boundaries |
| I-01 | PASS | 带2岁小孩：澄清儿童/婴儿，provider.search=0 |
| I-02 | PASS | 办签证：澄清签证/护照，不搜 |
| I-03 | PASS | 选座+里程卡：澄清不含指定座位/积分，不搜 |
| I-04 | PASS | 从杭州回：开口/缺口/多城，不收成北京上海往返 |
| I-05 | PASS | 已出票改签：不用新库存代替退改 |
| I-06 | PASS | 宠物随行+无障碍座位：不按普通成人票装满足 |
| I-07 | PASS | 「签证中心开会」模型判空列表则不拦 |

### 22.3 行为事实

- **判断归模型，后果归宿主。** 能力清单和「不能搜」是硬的；「用户是不是在要这件事」由抽取模型填 `unsupported_capabilities`。
- 成功抽取且列表为空：信任模型（避免「签证中心」假阳性）。抽取失败/跳过 LLM：才用用户文本正则兜底。
- `unsupported_capability:` 不会被 “unsupported → soft” 降级。
- Prompt 已写这些 code；改 prompt 后旧 D4 smoke 数字不可直接对比。

### 22.4 未做

- J D6 回流、D4 24 题计费 smoke
- 用户点「按成人单人继续」后的放行路径（本轮要求重说，避免装满足）
- 真人 DeepSeek 能力边界

---

*交接更新 2026-08-19。A–F 见 §19；G 见 §20；H 见 §21；I 见 §22；未测类型以 §18.3 J 为准。*

## 23. 2026-08-27：语义入口进评测 + LLM Judge + 前端去演示数据

**本轮不是补槽/红队轮。** 没跑计费模型，没碰 Duffel/LiteAPI，没创建 Test Order。
全部改动在离线确定性路径 + 前端 + 文档。

### 23.0 名词（本节新出现的先解释）

| 词 | 意思 |
|---|---|
| 旧链路 / legacy | 原有的「一句话 → 抽字段 → 合并进 `intent_fields`」路径 |
| 新链路 / semantic | ADR-0002 的「完整对话 → 一次解释 → 编译成查询」路径 |
| D15 | 本轮新增的评测门禁：同一份 `derived-v2` 分别走两条链路做并排对比 |
| 确定性替身 | 假装成模型的固定规则实现。不花钱、不联网、每次结果相同 |
| LLM Judge | 用另一个模型给「用户可见回复」的主观质量打 1–5 分 |
| rubric | 给 Judge 的评分细则表（`evals/rubrics/output-quality-v1.json`） |
| 校准 | 拿人工标注对齐 Judge 分数，验证 Judge 靠不靠谱 |

### 23.1 本轮发现的两个事实（重要）

1. **前端新建任务实际走的是旧链路。** `App.tsx` 调 `createLegacyNaturalLanguage`；
   `createSemanticNaturalLanguage()` 定义了但**无人调用**。追问路由是对的（按
   `task.intent_entrypoint` 分流）。后端语义适配器已装配，配了 Key 即可用。
   **与 `docs/intent-entrypoint-migration-summary.md` 的描述不符**——文档写的是
   「前端默认走语义入口」。**本轮刻意没翻转**：切默认入口是行为变更，ADR-0002 明确
   写了 legacy 保留是回滚保险。D15 数据已具备，等项目所有者拍板，改动量为一行。

2. **整个评测 harness 之前 0 覆盖新链路。** 所有 runner 都调
   `create_task_from_message()`。ADR-0002 removal gate 第 1、4 条**此前无法验证**。

### 23.2 产物

| 项 | 路径 |
|---|---|
| 分支 | `semantic-entrypoint-and-judge`（6 commits，工作树已清空） |
| 确定性语义替身 | `src/corporate_travel_agent/agent/deterministic_semantic_interpreter.py` |
| D15 runner | `examples/run_semantic_entrypoint_evaluation.py` |
| D15 首轮报告 | `reports/evaluation-runs/semantic-entrypoint-20260827/` **门禁 PASS** |
| Judge 核心 | `src/corporate_travel_agent/services/evaluation_judge.py` |
| Judge 适配器 | `src/corporate_travel_agent/services/evaluation_judge_openai.py` |
| Judge runner | `examples/run_output_quality_judge.py` |
| 新后端接口 | `GET /policy`、`GET /audit-events`（后者仅管理员） |
| 单测 | `tests/test_semantic_entrypoint_evaluation.py`（12）、`tests/test_evaluation_judge.py`（14）、`tests/test_api.py`（+3） |
| 协议 | `docs/evaluation-protocol.md` §3.1 D15 + §6.1 Judge 实现约束 |
| 清单 | `evals/manifest.json`：新增 D15、judge 段；`code_revision` 由 `null` 改为实际 revision |
| 沟通标准 | `AGENTS.md`（新建） |

复跑（输出目录必须不存在）：

```bash
.venv/bin/python examples/run_semantic_entrypoint_evaluation.py \
  --output reports/evaluation-runs/semantic-entrypoint-NEWDIR
```

### 23.3 D15 断言摘要

| 项 | 结果 |
|---|---:|
| D1 60 条工作流走语义入口，命中冻结期望 | **60/60** |
| 静默错搜（期望澄清却已产出方案） | **0** |
| D2 480 条 `premature_provider_call_rate` | **0** |
| D2 480 条 `inventory_hallucination_rate` | **0** |
| `clarification_accuracy` | 0.71875（与旧链路 delta = 0） |
| `out_of_scope_accuracy` | 0.10（与旧链路 delta = 0） |
| `missing_field_recall` | 0.3629（delta = 0） |
| `transport_preference_accuracy` | 0.9259（delta = 0） |

### 23.4 行为事实

- **替身能力刻意对齐。** 语义替身复用 `DeterministicChineseIntentParser._cities` /
  `._departure_date`，额外只认识 D1 的冻结英文模板语法。这样两条链路的差异只能归因
  于架构，不是「新替身更强」。改任一替身都会破坏这个前提。
- **`classification_accuracy` 在语义侧记 `not_applicable`，不是记低分。** 语义链路
  按设计没有场景分类器；反推该标签等于把 ADR-0002 删掉的东西装回来。唯一可比的分类
  结论是 `OUT_OF_SCOPE`，走 `out_of_scope_accuracy` 单独报告。
- **不同入口的观测不许合并。** `summarize_intent_observations()` 混入两种
  `entrypoint` 会直接抛 `EvaluationDatasetError`。
- **一条用例不许同时走两个入口。** `run_workflow_evaluation_case()` 同时收到
  `language_model` 和 `semantic_language_model` 直接报错。
- 顺手修了真 bug：模板路由正则 `[^.]+?` 在 `St. Louis` 的点号处截断，导致
  `prefer-compliant-0707` 误判缺 origin/destination。已锚定到下一句。

### 23.5 Judge 的硬规矩（都有测试守着）

| 规矩 | 说明 |
|---|---|
| 硬规则优先 | 硬失败用例照打分，但判定永久带 `hard_rule_failed` 并单独统计。高分不能洗白硬失败 |
| 弃权 ≠ 0 分 | 排除在均值外；全弃权返回 `None` |
| 维度必须完整 | 少给/多编维度 = 契约违规抛错，不是低分 |
| 盲评 | 每条输入调用前校验盲字段 |
| rubric 按 SHA-256 固定 | 每条判定记指纹；改 rubric 则历史结果显式不可比 |
| 校准三档 | `passed` / `failed` / `insufficient_samples` |

**校准现状必须记住：** `03-output-quality.jsonl` 的 20 条标注是
`annotator_id=human-01`、`round=1`，即**单标注者单轮**。rubric 要求的是
`required_human_double_rated_cases: 20`（人工双评）。因此**即使一致率 ≥0.9，
`calibration_status` 也必须是 `insufficient_samples`**，不得宣称已校准。
`tests/test_evaluation_judge.py::test_calibration_reports_agreement_against_human_labels`
把这条写死了。

**Judge 尚未真跑。** 只跑过 `--dry-run`：60 份输入、全部盲评合规、20 条标注全部可按
`run_id` 对齐。真跑需 `--confirm-billable-judge-calls`。

### 23.6 前端演示数据已清除

| 页面 | 之前写死的假数据 | 现在 |
|---|---|---|
| 我的差旅 | 4 个不存在的员工与行程、`¥28,640` 年度支出、`71%` 低碳出行 | `GET /trip-tasks` |
| 差旅政策 | 假版本 `CN-TRAVEL v3.2`、假指纹 `9f3ae711…7c12`、4 条编造规则 | `GET /policy` |
| 审计与系统 | 5 条假事件与假哈希、`工具预算 5/12` | `GET /audit-events` + `GET /health` |

三页均有显式 loading / error / empty 态。`GET /audit-events` 走任务投影 + `limit`
约束，不做全表 `list_tasks()` 扫描。

### 23.7 验收

```
pytest        590 passed（新增 29）
ruff          All checks passed
npm run build ✓
npm test      10 passed
```

**未逐个验证中间 commit 是否独立绿灯，只验证了 HEAD。** 原因：`api/main.py` 与
3 个前端文件同时含「语义路由」和「本轮新接口」两部分改动，本环境无交互式
`git add -i`，无法按行拆分。

### 23.8 未做

- ~~**前端切语义入口**（等拍板，一行）~~ → **已完成，见 §24.3**
- ~~§18.3 A–I 九类红队 runner 仍走 legacy~~ → **可移植的部分已接，见 §24.5**；
  本轮（§23）只接了 D1/D2 主集
- Judge 真实计费跑一次；找第二个标注者做真正的人工双评
- D6 回流仍 0；D3 真实快照仍 0/20
- CI 仍无评测门禁，只跑单测 + lint + PG 冒烟
- `recover_interrupted_tasks()` 仍全表 `list_tasks()`

---

## 24. 2026-08-27（第二轮）：前端切语义入口 + 真实跑 + 补齐测试

**一句话：** 前端已切到新链路并在浏览器里真跑通；真实跑 DeepSeek 抓到两个提示词缺陷
（已修）；语义入口的出错路径、持久化、红队用例补齐了测试。`pytest 590 → 614`。

### 24.0 名词

| 词 | 意思 |
|---|---|
| 信封结构 | 模型返回的 JSON 最外层长什么样：哪些字段在顶层，哪些在 `intent` 里面 |
| 脚本化替身 | 假装成模型、按预设剧本返回固定结果的测试用对象。不联网、不花钱 |
| 红队用例 | 专门用刁钻说法去撞系统的测试用例（§18.3 A–I） |

### 24.1 真实跑抓到的两个 bug（重点）

**这是本轮最有价值的产出。两个都只有真模型才会暴露，确定性替身永远测不出。**

**Bug 1｜模型把顶层字段塞进了 `intent` 里。**
DeepSeek 走 chat 接口（`response_format: json_object`），没有严格 schema 强制。
原提示词只丢了一份 `$defs` schema，没说清信封结构，模型就把 `evidence`、
`confidence`、`manipulation_detected` 等嵌进了 `intent` 内部 → 6 个 extra_forbidden
校验错误，整份解释作废。

**Bug 2｜多轮时证据丢失。**
用户最后一句只补了返程时间，模型就只给返程的证据，`origin`/`destination` 的引用没了
→ 宿主的「READY 必须有四项证据」规则把它拒了。**宿主规则是对的**，是提示词从没把这条
规则告诉模型。

**两次都是提示词缺陷，不是宿主 bug** —— 宿主两次都做了正确的事：停下、不查库存、
记下原因。这正是本轮新增的
`test_model_failure_pauses_for_structured_input_and_records_the_reason` 锁的行为。

修法：提示词显式写出顶层键列表、`intent` 的键列表、**绝不能出现在 `intent` 里**的键；
并写明 READY 时必须为 origin/destination/departure_after/arrive_by 各给一条证据，
**可以引用更早的轮次**。提示词版本 `semantic-trip-intent-v1 → v3`。

**D15 数字不受影响**：那轮用的是确定性替身 `deterministic-semantic-v1`，不是这个适配器。

### 24.2 修完后的真实端到端（上海→东京）

| 步骤 | 结果 |
|---|---|
| 建任务 | 模型读懂整句，但「9月13日晚上回来」太模糊 → **只问一句，0 次库存调用** |
| 回答追问 | 编译出往返两段查询 |
| 查库存 | 真实 Duffel 航班 + 真实 LiteAPI 酒店（沙箱只读） |
| 出方案 | 3 个方案，政策判定 `COMPLIANT` 并附规则证据 |
| 审计链 | `SEARCH_COMMAND_COMPILED` 确实排在进入 SEARCHING **之前** |

前端那一单（北京→大阪）返回「无可行方案」，是**正确结果**：沙箱返回了航班但没有一班
能在截止时间前到，并逐条列出被过滤的报价。没有幻觉，没有偷偷放宽时间窗。

### 24.3 前端已切语义入口

`frontend/src/App.tsx:1186`：`createLegacyNaturalLanguage` → `createSemanticNaturalLanguage`。

浏览器实测确认：服务端访问日志三次 POST **全部**打到 `/semantic/*`，`/legacy/` 零次。
旧链路仍保留为 ADR-0002 的回滚保险；历史 legacy 任务继续走自己的路由。

**注意：前端测试只覆盖 `src/utils/*.test.ts`，没有组件级测试，因此"新建任务走哪个
入口"这件事没有前端单测守着**，只有这次浏览器实跑作证。

### 24.4 新增测试（+24 条，590 → 614）

| 文件 | 条数 | 覆盖 |
|---|---:|---|
| `tests/test_semantic_entrypoint_reliability.py` | 11 | 模型挂了 / 引用对不上 / 预算用超 / 追问用尽 / 审计留痕 / 决策历史上限 |
| `tests/test_semantic_entrypoint_persistence.py` | 6 | 落库与重启恢复 / 重启后仍拒旧路由 / Provider 重试不重新解释意图 / 延迟重试 / 中断恢复 |
| `tests/test_api.py`（新增） | 4 | HTTP 端到端：建任务、追问、跨入口 409、模型挂了不返回"看起来已规划好"的任务 |
| `tests/test_semantic_intent.py`（新增） | 2 | 锁住 24.1 两个提示词修复 |
| `tests/test_semantic_entrypoint_evaluation.py`（新增） | 1 | 红队 runner 进 CI |
| `tests/semantic_fixtures.py` | — | 共享脚本化替身（不是测试文件，pytest 不收集） |

审计事件只存哈希不存明文，所以断言是拿 `stable_hash(期望值)` 去比对哈希。

### 24.5 红队用例走语义入口

**关键结论：A–I runner 不能机械移植，改一行是做不到的。**

旧 runner 把模型脚本成「什么都没抽到」，然后断言**宿主**的旧机器自己算对
（自己把「下下周三」解析成 9 月 2 日、自己把「酒店不要了」翻译成清空住宿字段）。
ADR-0002 把这些机器从语义链路里**删掉了**——这些职责在新链路里归模型。

新建 `examples/run_semantic_redteam_acceptance.py`，只移植**在新链路里仍归宿主**的职责：

| 组 | 条数 | 内容 |
|---|---:|---|
| SI | 8 | 能力边界：模型判 UNSUPPORTED，宿主必须披露原因且**一次库存都不查**；含 2 条对照组 |
| SG | 6 | 改口：整份重新编译，不留旧值，旧方案作废，不做多余的搜索 |
| SH | 3 | 读不准就别查：二选一日期 / 二选一城市 / 条件住宿 |

`17/17 PASS`，报告在 `reports/evaluation-runs/semantic-redteam-20260827/`。
`tests/test_semantic_entrypoint_evaluation.py` 里有一条单测把它拉进 CI。

**明确没有移植的（runner 和报告里都写死了）：**

1. **§18.3 H-01/03/04/05 日期解析正确性**（下下周三、8/5 与 8.5、春节不编公历、
   跨年）。它已经进了模型，用脚本化替身断言等于自己写答案自己批改。要验只能真跑
   计费模型。
2. **§18.3 I 的正则兜底**。语义链路没有兜底层，模型不可用时直接停在结构化表单。
3. **§18.3 A–E / F**。走结构化入口或根本不经过意图链路，与入口无关。

### 24.6 提交

分支 `semantic-entrypoint-and-judge`，5 个 commit，工作树干净：

```
5b50045 feat(frontend): create new tasks through the semantic entrypoint
3cd98c2 test(semantic): run the portable red-team cases through the new entrypoint
1da4dda test(semantic): cover persistence, restart recovery and provider retry
3ddd4ac fix(intent): make the semantic prompt state the response envelope
55e8aae test(semantic): cover the entrypoint's failure and audit paths
```

### 24.7 验收

```
pytest        614 passed（新增 24）
ruff          All checks passed
npm run build ✓
npm test      10 passed
npm run lint  ✓
真实跑        DeepSeek + Duffel/LiteAPI 沙箱，端到端出方案；浏览器实跑确认走语义入口
```

### 24.8 本轮顺带发现，**未改**

**`config/travel-policy.json` 的城市表里没有大阪。** 东京/北京/上海都有别名会规范成
英文，大阪没有，所以一路是中文「大阪」。Duffel 仍能解析，不影响结果。加城市属于政策
数据变更（酒店限价按城市键），需要项目所有者决定。

### 24.9 仍未做

- **Judge 真实计费跑**仍为 0；人工双评仍缺第二个标注者，`calibration_status` 必须
  继续报 `insufficient_samples`
- **日期解析正确性在语义链路上没有任何验证**（见 24.5 第 1 条），这是当前最大的
  测试空白，需要一次计费模型跑
- 前端无组件级测试，入口选择靠人工验证
- D6 回流仍 0；D3 真实快照仍 0/20
- CI 仍无评测门禁，只跑单测 + lint + PG 冒烟
- `recover_interrupted_tasks()` 仍全表 `list_tasks()`
- 本轮真实跑用的是内存存储（本机 Postgres 没起），未在 PG 上真实跑过语义任务

---

## 25. 2026-08-27（第三轮）：日期解析真跑 + 三个修复

**一句话：** 语义链路的日期解析第一次有了真实模型数据（`7/8`）；过程中改坏过两次，
都留了证据；顺带修掉三个真问题。`pytest 623`。

### 25.0 名词

| 词 | 意思 |
|---|---|
| 信封结构 | 模型返回的 JSON 最外层长什么样：哪些字段在顶层，哪些在 `intent` 里 |
| 结构修复 | 只把放错位置的字段搬回该在的地方，不改任何取值 |
| 证据 | 模型给的「这个字段是用户哪句话说的」引用 |

### 25.1 新增：日期解析的计费评测

`examples/run_semantic_calendar_model_evaluation.py`，把 §18.3 H 的 8 条日期用例
喂给真实模型走语义入口。库存用 Mock，**只花模型的钱**，一轮约 $0.010。

默认是**测量不是门禁**（失败不退非零码），加 `--gate` 才当门禁。

### 25.2 四轮跑的轨迹（全部留档，包括改坏的两轮）

| 报告目录 | prompt | 结果 | 失败调用 | 说明 |
|---|---|---:|---:|---|
| `semantic-calendar-model-20260827` | v3 | 6/8 | 1 | 首轮基线 |
| `...-v4` | v4 | 3/8 | 1 | **我改坏了**：年份规则过度触发，模型对每个日期都问年份 |
| `...-v5` | v5 | 2/8 | 5 | **仍然坏**：「别问」被模型泛化成「什么都别问」，撞上证据规则 |
| `...-final` | v6 | **7/8** | **0** | 收敛 |

**改坏的两轮刻意保留。** 它们是这三个修复的证据来源，删掉就没法解释为什么这么改。

### 25.3 三个修复

**① 信封结构修复。** 没有严格 schema 强制的接口（DeepSeek `json_object` 模式）偶尔
把顶层字段嵌进 `intent` 里——8 次中 1 次。适配器现在把这些键**原样搬回顶层**再校验
一次，只搬位置不改取值，因此不构成「改写旅行者的意思」；修过就在调用元数据里记
`envelope_repaired`。修不好的一律改判**可重试**，不再一次判死。

**② 「证据不全」不再是死路。** 此前 READY 缺任一必填字段的证据 = 模型违约 → 用户
被推去填结构化表单。可**大多数句子根本不会说到达时限**，正确读法是「这件事还没问
清」。现在这些字段进 `missing`，宿主追问。伪造引文、引用不存在的轮次或助手发言，
**仍然是硬违约**。

**③ 过去的日期确定性拒绝。** 语义链路此前**从头到尾没有任何「日期是否已过」的检查**
——`validate_trip_request_values` 只查类型、时区、先后顺序，从不和「现在」比。现在
出发/返程日历日早于今天就直接给出「日期已过：…请改成今天之后的日期」，不搜库存，
且这句结论**盖过模型自己的追问**。比的是**日历日不是精确时刻**，所以「今天早上出发、
下午来问」不会被误判过期。

### 25.4 按项目所有者定的年份规则

> 没写年份默认今年；如果那天已经过去，问用户是不是明年；答明年或更晚就继续；
> 答今年那个已过去的日期或更早，就报错告诉用户日期已过。

- 「默认今年 + 已过则问年份」写进提示词（v6，两个方向都给了例子）；
- 「答了过去的日期就报错」由宿主确定性实现（修复③），不依赖模型自觉。

**H-03d 的期望值按这条新规则重写过**，不再等同旧链路。旧链路是「整条日期留空」；
新规则下**问年份**和**报日期已过**都算过，因为两者都满足真正的红线：绝不悄悄顺延到
明年、绝不拿过去的日期去搜库存。runner 里写了为什么改。

### 25.5 唯一还没过的：H-01「下下周三」

模型不肯自己定这个日期，只肯问：「下下周三可能是 2026年9月2日，请确认」——
**它算对了，但不肯拍板。**

这是新旧链路的真实能力差：旧链路有确定性的中文相对日期解析器，语义链路把这件事
交给了模型。行为是安全的（问而不猜），但比旧链路多一轮。**没有为了刷分去改它。**

### 25.6 验收

```
pytest        623 passed（新增 9）
ruff          All checks passed
npm run build / test  ✓
真实评测      7/8，8 次调用 0 失败，约 $0.010
```

### 25.7 仍未做

- H-01 相对日期：要么给提示词加中文相对日期示例再跑，要么接受多问一轮
- 四轮累计约 $0.031，**失败调用照样计费但拿不到 usage**，台账偏低
- Judge 真实计费跑仍为 0；人工双评仍缺第二个标注者
- 前端无组件级测试；`config/travel-policy.json` 仍无大阪
- CI 无评测门禁；语义任务未在 Postgres 上真实跑过

---

## 26. 2026-08-28：日期解析 8/8 稳定 + 已起飞库存不再出现 + 信封三层防护补齐

**一句话：** 日期解析做到 **8/8，每条重复 3 次全过**；修掉"下午能订到上午已起飞航班"
这个两条链路共用的真 bug；信封防护补齐第三层并有测试守着。`pytest 630`。

### 26.1 「模型算对了却不肯拍板」——是我的提示词把它弄胆小了

有数据：H-01「下下周三」在 **v3 通过**，v4/v5/v6 **连续三版失败**，正是我加日期歧义
段落之后开始的。模型算出了 2026-09-02，却只肯反问「是不是这一天」。

**机制：** 「读不准就问」这条规矩会外溢。我为年份歧义写的段落被模型泛化成
「日期这件事都该问一下」，连自己算得出来的相对日期也不敢定。

**修法：** 加一条方向相反的硬指令——算得出唯一答案的相对日期（下下周三、后天、
下个月15号）**归你算、归你拍板**，不许把算术推回给用户，也不许让用户确认你已经算出
来的日期；同时保留「真有两种读法才问」的边界（这周五还是下周五、农历、年份规则）。

**结果：** H-01 **3/3 通过**。

### 26.2 顺带挖出 `8/5` 的日/月顺序问题

重复跑发现 `8/5` **0/3**，而同义的 `8.5` **3/3**。模型把 `8/5` 读成了 **5月8日**——
那确实在参照时刻之前，于是年份规则**正确地**触发了。**错的是日期顺序，不是年份规则。**

提示词现在钉死中文语境的月在前约定（`8/5`、`8.5`、`8-5`、`8月5日` 都是 8 月 5 日），
并要求**先定月日、再判年份**。修完 **3/3**。

### 26.3 「下午订上午票」是真 bug，两条链路都有

`FeasibilityValidator` **从头到尾不和「现在」比**：只对照旅行者的窗口。而
`departure_after` 是**下界**——下午两点搜「今天从北京去上海」，上午九点那班满足
「今天出发」，但它已经飞了，照样进方案。

**修法：**
- `validate()` 新增**必填**参数 `now`，已起飞（`depart_at <= now`）的去程/返程直接不可行；
- 编译期额外按**精确时刻**拒绝已经过去的到达时限（这是同一问题的另一半）；
- `now` 做成必填而不是可选，避免哪个调用方不小心跳过这道检查。

**回放冻结证据时不做存活性检查**：「这班已经飞了」是给实时下单用的护栏，重算归档
数据的政策结论问的是「当时判得对不对」。回放锚点取自数据本身（最早一班出发前一秒），
而不是给校验器开一个会被误用的旁路开关。

### 26.4 连带发现：演示夹具早就过期了

演示库存写死在 2026-08-05。真实时间越过那天之后，**整份演示数据全部不可行**——
以前没人和「现在」比，所以一直没暴露。现在 `demo.py` 导出 `DEMO_CLOCK`，用这份冻结
库存的调用方显式传它；接真实 Provider 的调用方继续用真实时钟。

### 26.5 信封三层防护补齐

| 层 | 内容 | 守着的测试 |
|---|---|---|
| 1 提示词 | 说清顶层键、`intent` 的键、绝不能进 `intent` 的键 | `test_chat_mode_prompt_spells_out_the_envelope...` |
| 2 结构修复 | 放错位置的顶层字段原样搬回；只搬位置不改取值；记 `envelope_repaired` | `test_chat_mode_lifts_decision_keys...` |
| 3 重试 | 修不好 → 可重试 → **上层真的重试一次** | `test_a_malformed_envelope_is_retried_and_the_task_still_completes` |

**这次补的是第 3 层的证明。** 之前只是把错误标成"可重试"，没有任何测试证明重试真的
发生。现在有两条：一条证明重试后任务能走完（模型被调用 2 次、失败那次也留痕），
一条证明重试**有上限**（一直坏就停在结构化表单，不会无限重来）。

第 2 层也扩到了 Responses 路径，两个 API 模式共用同一套修复。

### 26.6 runner 加了 `--repeats`

单跑一次的分数是**噪声**：v7 那轮 H-03a 挂了、H-03b 过了，而这俩是同一个日期的两种
写法。重复 3 次才分得清真实退步和模型抖动，报告里对「时好时坏」的用例会单独点名。

### 26.7 最终结果

```
reports/evaluation-runs/semantic-calendar-model-20260828-final/
8/8 PASS，每条 3/3，24 次调用 0 失败，约 $0.033
prompt v8；pytest 630；ruff / 前端 全过
```

四版提示词的轨迹（含我改坏的两版）全部留档，目录见 §25.2 与 `reports/evaluation-runs/`。

### 26.8 仍未做

- 累计计费约 $0.12。失败调用照样计费但拿不到 usage，台账偏低
- Judge 真实计费跑仍为 0；人工双评仍缺第二个标注者
- 前端无组件级测试；`config/travel-policy.json` 仍无大阪
- CI 无评测门禁；语义任务未在 Postgres 上真实跑过
- 日期评测只跑过 DeepSeek 一个模型，换模型需重测

---

## 27. 2026-08-28（第二轮）：真实 Judge / Postgres 语义并发 / 政策哈希 bug

**一句话：** LLM Judge 第一次真跑（60 次调用，均分 4.55）；语义任务在真 Postgres 上
跑通并抓到一个**发一次版就锁死所有在途任务**的严重 bug；双 worker 并发验收 2/2。
`pytest 634`。

### 27.1 政策哈希在进程之间不稳定（本轮最严重）

**症状：** 真 Postgres 上建任务 → 杀进程重启 → 追加消息 → **409 历史政策快照内容已变更**，
而政策文件一个字没改。

**根因：** 政策里 `exception_allowed_rule_ids` 是**集合**。集合无序，Python 每个进程的
字符串哈希种子不同，序列化顺序就变；算哈希用的 `sort_keys=True` **只排字典的键、不排
列表的值**。于是同一份政策每个进程算出不同哈希。实测同一 ID 出现三个哈希。

**后果：** 这个检查本来防"任务在跑时有人偷改政策"。结果它**每次普通重启都误报**，把所有
在途任务锁死；真正的篡改反倒只能靠运气抓到。**这个安全检查是坏的。**

**修法：** 算哈希前把集合字段排序。回归测试**必须开子进程**——同一解释器里哈希种子固定，
自己跟自己比永远一致，测不出来。

**为什么只有真库能发现：** 内存存储里任务随进程消失，没有任何代码会去重读一个存过的
哈希。这正是"在 Postgres 上跑"的价值——不是验数据库能不能连，是让**重启**变得可观测。

### 27.2 语义任务在真 Postgres 上端到端

真 DeepSeek + 真 Duffel/LiteAPI 沙箱 + Postgres：建任务（3 个真实方案）→ 杀进程 →
重启 → 追加「酒店不要了，我住朋友家」→ 住宿清空、只重搜交通、3 个方案全不带酒店。
落库内容核对无误：`intent_entrypoint=semantic`、语义判定 READY、中文原话作证据、
`prompt_version=semantic-trip-intent-v8`、`envelope_repaired=False`。

### 27.3 双 worker 并发验收 2/2

`examples/run_postgres_semantic_dual_worker_acceptance.py`，独立库、0 计费调用、Mock 库存。

**为什么语义任务要单独验并发：** 语义链路比结构化多了**对话账本**。两个进程同时追加
消息时，最危险的不是状态机乱掉，而是**某一轮用户发言被悄悄覆盖**——之后每次重新解释
都基于一份残缺对话，且不报错。

**断言的是任何交错都必须成立的不变量，不是某一种先后：**

| 用例 | 结果 |
|---|---|
| SD-01 两进程同时追加消息 | 一方成功、一方拿到 `ConcurrentUpdateError`；库里正好 2 轮用户发言，不丢不重；入口仍是 semantic；判定历史条数与轮次一致 |
| SD-02 到期重试被抢 | 只有 worker-a 领走，worker-b 空手；库存没搜两遍；**重试不重新解释意图**（判定历史仍为 1） |

写这个 runner 时踩了自己一个坑：workflow 时钟冻结在 2026-08-01，却按真实时间设到期
时刻，于是永远"还没到期"，两个 worker 都拿不到活干。已在代码里注明。

### 27.4 LLM Judge 第一次真跑

`reports/evaluation-runs/judge-output-quality-20260828/`，60 次调用、11.4 万 token、5 分钟。

| 项 | 结果 |
|---|---|
| 平均分 | **4.55 / 5** |
| 弃权 | 5（排除在均值外，不记 0 分） |
| 硬失败被高分洗白 | **0** |
| 严格一致率 | 57.6% |
| 邻近一致率（差≤1 分） | **97.6%**（目标 90%） |

分维度：政策透明度 / 证据落地 邻近一致 **100%**；「不确定性与失败的诚实度」严格一致
只有 29% 但邻近 100%——两边排序一致，只是评委手松手紧。这正是校准要解决的。

### 27.5 单标注者校准模式（项目所有者要求）

原本两道门：**人工双评**、**样本量 ≥20**。所有者要求忽略双评那道。

实现为**显式开关** `--accept-single-annotator`：

- 只放行双评这一道，其他一概不动；
- 样本量改按**人工标注条数**算，而不是按"评委没弃权的条数"算——弃权是评分表明确允许
  的行为（弃权≠0分），不该反过来把数据集判成太小；
- 状态命名为 `passed_single_annotator` / `failed_single_annotator`，**永远不会显示成
  普通的 `passed`**。一致率再高，单人打的分也只能说明「评委和这个人想的差不多」，
  不能说明这个分数客观。
- 默认行为不变；有测试守着"放行不等于放水"（一致率不达标照样 failed）。

用现有 60 份判定**离线重算**（0 次新调用）：**`passed_single_annotator`**，
邻近一致 0.98 / 目标 0.90，17 条可比对 + 3 条评委弃权。

### 27.6 验收

```
pytest 634 / ruff 全过
真实 Judge      60 次调用，均分 4.55
Postgres 端到端  真模型 + 真 Provider + 跨进程重启，通过
双 worker 并发   2/2，0 计费调用
```

### 27.7 仍未做

- Judge 真正的人工双评仍缺第二个标注者（本轮是显式放行，不是解决）
- CI 无评测门禁，待定花钱策略（合并时跑 / 每晚跑）
- 前端无组件级测试；`config/travel-policy.json` 仍无大阪
- 日期评测只跑过 DeepSeek 一个模型

---

## 28. 2026-08-28（第三轮）：多轮对话 + 多段行程，抓到静默错搜

**一句话：** 多轮 + 多段行程评测 **5/5（每条 2 次全过）**；过程中抓到一个**静默错搜**
（开口程被压成用户没要过的航线并真的搜了库存）和一个我自己上一轮改出来的回归。
`pytest 640`。

### 28.1 系统能表达什么行程（先说清楚）

`transport_legs()` 把返程**推导**成「目的地→出发地」，所以领域模型只能表达两种形态：

- **单程**
- **原路往返**

**多城**（北京→上海→杭州→北京）和**开口程**（去上海、从杭州回）**表达不了**。
所以这两种必须被拦住，而这里最危险的不是报错，是**悄悄压缩**。

### 28.2 静默错搜（本轮最严重）

真模型实测，用户明确说「去程9月15号北京飞上海，**返程9月20号从杭州飞回北京**」：

```
去程  Beijing → Shanghai   ✅
返程  Shanghai → Beijing   ❌ 用户说的是 杭州 → 北京
搜库存 2 次                ❌ 拿一条用户没要过的航线去搜了
```

**杭州被无声丢掉。** 这正是 ADR-0002 列为历史危险的「把开口程压缩成单一路线」。

**根因是结构性的：** `SemanticIntent` **没有字段能表达「返程从别的城市出发」**，
模型读到了也没地方放，于是丢了。

**修法按项目架构走（AI 只负责读懂，判定归确定性代码）：**

1. `SemanticIntent` 新增 `return_origin_candidates`；
2. 提示词告诉模型：**如实记下这座城市，哪怕这趟因此订不了**——「丢掉旅行者说过的
   城市」是唯一绝对不许做的事，「能不能订」是宿主的活不是你的活；
3. `compile_search_command` 确定性拒绝：返程出发城市 ≠ 目的地 → 报「行程形态做不了」，
   不搜库存，且这句结论盖过模型自己的追问。
4. 普通往返（模型如实记「从上海回」而上海就是目的地）不受影响，有测试守着。

### 28.3 长对话被反复追问已经说清的事（我上一轮改出来的回归）

五轮对话，模型把出发地、目的地、日期**全读对了**，系统却还在问
「请确认这些出行信息后我再搜索：origin、destination」。两个独立原因：

**① 证据契约要求了一个 schema 里不存在的字段名。**
`SemanticIntent` 里的真名是 `origin_candidates` / `destination_candidates`，
宿主却只认 `origin` / `destination`。模型照 schema 写真名——**完全合理**——却被判成
「一条证据都没给」。`departure_after` / `arrive_by` 碰巧两边同名，所以只有城市中招。
**要求一个不存在的名字是宿主的坑。** 现在两种写法都认，提示词也改成说 schema 真名。

**② 证据不累积。** 出发地目的地通常在第 0、1 轮就说清了，到第 5 轮模型只引用最新
那句。账本是累积的，证据也应当累积：某字段在本任务某一轮被原话落实过、**且取值至今
没变**，就仍然算落实；**取值一变就必须重新拿出原话**（有测试守着）。

### 28.4 评测结果

`reports/evaluation-runs/semantic-multiturn-20260828-final/` · 28 次调用 · 约 $0.044

| ID | 结果 | 内容 |
|---|---|---|
| MT-01 | 2/2 | 五轮逐步搭出往返，每轮信息都不丢 |
| MT-02 | 2/2 | 改目的地，旧目的地不残留 |
| MT-03 | 2/2 | 多城行程：0 次搜库存 |
| MT-04 | 2/2 | 开口程：0 次搜库存，不编造返程航线 |
| MT-05 | 2/2 | 第四轮推翻第一轮，后说的盖住先说的 |

轨迹：3/5 → 4/5 → **5/5**，中间两版分别修掉上面两个缺陷，全部留档。

### 28.5 再次确认：城市别名表太小

MT-02 的目的地一路是中文「广州」，而北京/上海会规范成英文——`config/travel-policy.json`
的城市表只有 10 个左右，广州、大阪都不在里面。**系统会半英半中地继续跑下去**，
不报错。用例已改成两种写法都接受（它测的是「旧值不残留」不是译名），缺口单独记在这里。
加城市属于政策数据变更（酒店限价按城市键），需要项目所有者决定。

### 28.6 验收

```
pytest 640 / ruff 全过
多轮评测 5/5（每条 2 次），28 次调用约 $0.044
prompt v8 → v10
```

### 28.7 仍未做

- 城市别名表补全（广州、大阪等），需所有者拍板
- 多城行程目前只是「拦住」，没有「拆成多个申请」的产品路径
- Judge 真正的人工双评仍缺第二个标注者
- CI 无评测门禁，待定花钱策略

---

## 29. 2026-08-28（第四轮）：承诺、政策分档、有序航段

**一句话：** 落地了规划架构的前三步。**开口程从"被拒绝"变成"真的支持"**，
政策从"权重+静默过滤"变成"分档+带理由保留"，会面地点不再被丢掉。
`pytest 646`，多轮真实评测 **5/5（每条 2 次）**。

### 29.1 第一步：承诺不再被丢掉

| 东西 | 之前 | 现在 |
|---|---|---|
| 会面地点 | 模型抽出来 → 进 intent_fields → 澄清还会追问 → **编译时整个丢掉** | `TripRequestVersion.client_location` |
| 会前必须到 | 硬约束元组里的**一个字符串**，可行性校验靠特判 | `Commitment.safety_buffer_required` |
| 到达时限 | 单独躺在 `arrive_by` | `Commitment.not_later_than` |

三样本来是同一件事——**某时某地我必须在场，为了什么**。拆散之后政策只能查单价，
永远没法判断这趟差旅本身合不合理。现在收拢成 `Commitment`，随请求落库、经 API 暴露。

### 29.2 第二步：政策从"权重 + 静默过滤"改成"分档 + 带理由保留"

**改掉的两个错位：**

1. **政策不再进分数。** 之前 `policy_penalty = 1000 if 需审批` 直接加进 score，
   于是一个需审批但便宜 1100 的方案会排在完全合规的前面——**价格把政策投票推翻**。
   现在排序是「先按政策分档，档内再按分数」，政策**完全不参与打分**。
2. **被禁方案不再静默消失。** 之前 `continue` 直接丢掉，用户只会看到一份莫名偏贵的
   列表，永远不知道最便宜那个是被政策禁的。现在带着 `violation_ids` 保留。

**保留是为了透明不是凑数：** 只有当被挡方案的分数**优于所有可选方案**时才保留
（那正是"最便宜的被禁了"需要说出口的情形）；比可选方案还差的只是噪音，不保留。

**安全性没有放松：** `select_option` 本来就有 `INV-003 非合规方案不得推进`，
被禁方案出现在列表里也选不了。

### 29.3 第三步：行程变成有序航段列表

`TripRequestVersion.journey: tuple[TripLeg, ...]`。**单程 1 段、往返 2 段——类型是数
出来的不是声明的。** 为空则按旧扁平字段推导，所以**冻结评测数据和已落库载荷全部继续有效**
（expand 阶段，没有 contract）。

新增确定性校验 `_journey_conflicts`：段数上限 **6**（抄 Amadeus，把"做不了"变成可判定
的数字）、必须串接（上一段落地早于下一段起飞）、时间窗必须有序且带时区。

### 29.4 直接后果：开口程从"拒绝"变成"支持"

上一轮为堵静默错搜加的拒绝逻辑**已经删除**。返程起点现在就是第二段自己的 `origin`。

真实模型实测（MT-04，2/2）：

```
用户：去程9月15号北京飞上海，返程9月20号从杭州飞回北京
航段：[('Beijing','Shanghai'), ('杭州','Beijing')]   ← 第二段起点确实是杭州
```

可行性校验也跟着改了：返程航线按**行程里声明的那一段**校验，不再硬套"目的地→出发地"。

**两条期望值因此变更**（都在原地写明了原因，是能力变了不是把测试掰弯）：
`test_an_open_jaw_return_is_refused...` → `..._becomes_its_own_leg...`；
红队 SI-04 的能力边界从"开口程做不了"收窄到"三段以上做不了"。

### 29.5 验收

```
pytest 646（新增 6）/ ruff / 前端  全过
红队 runner 17/17
多轮真实评测 5/5（每条 2 次），27 次调用约 $0.042
prompt v10 → v11
```

### 29.6 城市别名表：第三次咬人

`杭州` 没被规范成 `Hangzhou`（前两次是大阪、广州）。`config/travel-policy.json` 的城市表
只有 10 个左右。**这次已经不只是显示问题**——不在表里的城市，真实 Provider 能不能查
还没验证过（本轮用的是 Mock 库存）。相关用例已改成接受中英两种写法，
但**补表这件事建议尽快拍板**。

### 29.7 后续步骤（架构文档里的 04–06，均未做）

- 04 要求与偏好加**作用域**（"这一段直飞"而不是"全程直飞"）；顺手清掉三个空转偏好
  （`lowest_cost` / `shortest_duration` / `compare_train_and_flight` 在排序代码里根本没出现）
- 05 规划器从过滤器变成产生器（先枚举**走法**再选报价）
- 06 接上「按分歧提问」的循环——**这一步之后系统才真正会规划**

架构方案全文见本轮产出的设计文档（Artifact《会规划的差旅助手》）。

---

## 30. 架构方案与六步计划（**常读章节，非会话日志**）

> 这一节和别的章节不一样：**它不是某次会话的记录，而是当前正在执行的计划**。
> 每完成一步就回来更新进度表。设计文档全文另有 Artifact《会规划的差旅助手》，
> 但本节自包含——**不看那份文档也能照着做**。

### 30.0 名词

| 词 | 大白话 |
|---|---|
| 承诺 Commitment | 我必须在某地、某时之前出现，为了什么 |
| 走法 Plan | 具体怎么走：每段坐什么、几点、住哪 |
| 航段 Leg | 一次起讫（北京→上海）。单程 1 段、往返 2 段、多城 N 段 |
| 分档 | 政策三档（合规 / 需审批 / 禁止）排序时先分档，档内再比分数 |

### 30.1 一句话架构

**把「要做到的事」和「怎么做到」彻底分开，中间放一个循环。**

- **要做到的事 = 承诺**：模型从原话读出来，**允许带着不确定**
- **怎么做到 = 走法**：确定性代码算出来，一次算出**几个明显不同的走法**
- **中间是循环不是流水线**：先试着规划，只有当「不知道的那件事真的会改变结论」时才回头问

### 30.2 为什么不是「算出最优解」

差旅没有唯一的最好——便宜的起得早，舒服的贵。**把这些揉成一个分数，等于替旅行者做了
他自己该做的取舍。** 规划器要产出的是**几个真正不同的走法，每个都是它那一类里最好的**，
并说清楚彼此差在哪，取舍交回给人。

### 30.3 三条铁律

1. **模型只读原话和讲人话**，中间的规划、可行性、政策判定一行都不交给它。
   追问**问什么**由代码决定，**怎么问**才交给模型措辞。
2. **政策是标注，不是过滤器，更不是分数。** 过滤是展示层的决定，不是规划层的决定。
3. **不确定一路保留到它真的要紧为止**（ADR-0002 那条原则在规划层的延伸）。

### 30.4 六步进度

| # | 步骤 | 状态 | 落点 |
|---|---|---|---|
| 01 | 承诺不再被丢掉 | ✅ §29 | `domain/models.py` `Commitment`；`search_command.py` `_commitments_from` |
| 02 | 政策分档 + 被禁方案带理由保留 | ✅ §29 | `planning/planner.py` `_policy_band` / `_select_ranked_options` |
| 03 | 行程变成有序航段列表 | ✅ §29 | `TripRequestVersion.journey`；`validation.py` `_journey_conflicts`（上限 6 段） |
| 04 | 要求与偏好加**作用域** | ⬜ **下一步** | 见 30.5 |
| 05 | 规划器从过滤器变成**产生器** | ⬜ | 见 30.6 |
| 06 | 接上**按分歧提问**的循环 | ⬜ | 见 30.7 |

**01–03 已经各自独立产生价值**，即使 04–06 永不执行也不算白做。
**04 是 05 的前置**：没有作用域，枚举出来的走法没法正确评分。

### 30.5 第 04 步：要求与偏好加作用域

**要解决的三个具体缺陷（都已核实）：**

1. **偏好只看去程。** `planner.py::_preference_penalty(request, outbound, hotel)`
   **压根没接收返程**。往返里说「优先高铁」，只有去程被评分。段数一多成比例放大。
2. **三个软偏好是空转的。** `lowest_cost`、`shortest_duration`、`compare_train_and_flight`
   在排序代码里**根本没出现**。前两个的效果已无条件包含在 score 里，
   所以**声明与不声明毫无区别**。
3. **约束没有作用域。** `direct_only` 是个全局字符串，没法表达「去程直飞就行、返程无所谓」。

**怎么做：**
- 约束与偏好从 `tuple[str, ...]` 变成带作用域的结构：作用于**全程**或**某一段**
- `_preference_penalty` 改成接收**整个走法**（所有航段 + 所有住宿），不再只收去程
- 声明了却没有对应评分维度的偏好**不允许存在**——要么实现，要么从词表里删掉
- **受控词表不要动**：模型只能从白名单里选，不能自己发明约束。分层解决的是「归错类」，
  不是「放开写」

### 30.6 第 05 步：规划器从过滤器变成产生器

今天是「拿到报价 → 笛卡尔积 → 过滤 → 排序取前 3」，而且**返回的三个方案常常是
同一个走法的不同价格**（`_display_fingerprint` 只按报价编号去重）。

**改成：** 先枚举**走法**（飞还是高铁、在哪过夜、从哪回）→ 每种走法各自选报价 →
按类别各取最优 → 政策分档。

**关键论证：这不是旅行商问题。** 承诺自带时间，**顺序基本被时间钉死**
（16 号上海一定在 18 号杭州之前），所以要解的是「在相邻两个已定承诺之间，
选交通方式与衔接点」。段数上限 6 的前提下穷举可行。

**开工前必须先量一次：** 三段行程实际会枚举出多少种走法、每种要花多少次库存查询。
**不要凭这段话就开工。**

### 30.7 第 06 步：按分歧提问的循环

**今天的判断依据是「字段填满了没有」——这是填表不是规划。**
实测：五轮对话系统一轮问一个字段问了五次，其中好几个问题的答案根本不改变最终推荐哪班车。

**改成：**

```
1 读对话  → 承诺/要求/偏好，不确定的原样保留，不猜
           ↓ 带着不确定往下走，不停下来问
2 试着规划 → 把每个不确定的可能性摊开各算一遍，得到一批候选走法
           ↓ 比较候选
   ├─ 结论一致 → 不问了，直接给方案
   └─ 分成两派 → 只问那一件造成分歧的事
           ↺ 拿到回答回到第 1 步
3 落到报价 → 走法定下来才查库存、比价、跑政策，最后由模型讲成人话
```

**这一步几乎不花钱：** 「会不会改变结论」大多数时候**不用调任何外部接口**——
路线在时间上成不成立、要不要多住一晚、能不能改坐高铁，靠地理和时间就能算。
只有最后排序才需要真实报价。

### 30.8 什么时候应该停下来

**如果产品定位其实是「一张更聪明的搜索表单」**（用户已经决定路线，系统负责查和校验），
那么做到第 03 步就够了，**第 05、06 步是浪费**。

这个前提要项目所有者确认（见 §1.5 第 2 条），**不要替他假设**。

### 30.9 相关评测 runner

| runner | 验什么 | 花钱 |
|---|---|---|
| `run_semantic_redteam_acceptance.py` | 红队用例走语义入口的宿主职责 17 条 | 0 |
| `run_semantic_multiturn_model_evaluation.py` | 多轮 + 多段行程，含开口程与多城 | 每轮约 $0.02×repeats |
| `run_semantic_calendar_model_evaluation.py` | 中文日期边角 8 条 | 每轮约 $0.01×repeats |
| `run_postgres_semantic_dual_worker_acceptance.py` | 真库并发：账本不丢轮次、重试只被一个 worker 领走 | 0 |
| `run_semantic_entrypoint_evaluation.py` | D15 新旧链路并排对比 | 0 |

**计费 runner 一律需要 `--confirm-billable-model-calls`，且输出目录必须不存在。**
**单跑一次的分数是噪声**，用 `--repeats` 区分真实退步和模型抖动。

---

*交接更新 2026-08-28。*

**新 session 只需读两节：§1 现状，§30 架构方案与六步计划。** 其余是历史记录，按需查。
**§30 是常读章节，每完成一步回去更新进度表。**

*沟通标准见 `AGENTS.md`：先解释名词再用，先给结论再给细节，诚实优先于漂亮。*
*本轮见 §29；上一轮 §28；再上 §27、§26、§25；A–F 见 §19；G 见 §20；H 见 §21；I 见 §22。*
