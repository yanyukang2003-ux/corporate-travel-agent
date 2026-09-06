# 企业差旅规划与合规 Agent

这是根据《企业差旅Agent-需求基线》和《企业差旅Agent-V1架构设计》新建的 V1 框架。它用单 Agent 编排任务，用确定性代码校验行程与政策，并把库存来源、规则版本、审批对象和状态变化写入审计轨迹。

项目当前是可运行的工程骨架，不声称已经接入真实携程/12306 权限，也没有生产支付、
出票或退改签能力。Duffel 仅接入 Test Mode，并提供受显式门禁保护的测试订单创建与取消。

## 已实现

- 完整行程组合：去程、返程、酒店先组合，再执行硬约束过滤和排序；
- 版本化员工、请求、政策、方案和审批对象；
- `COMPLIANT / REQUIRES_APPROVAL / FORBIDDEN / INSUFFICIENT_EVIDENCE` 四类政策结果；
- 每条政策判断保存规则 ID、实际值、阈值、政策版本和例外权限；
- 受状态机约束的选择、审批、重验和官方平台交接意图；
- Mock Provider 的超时、涨价、售罄与重验失败注入；
- Replay Provider 的不可变快照回放；
- Booking Intent 幂等键和全流程审计事件；
- OpenAI function-calling 工具循环适配器（`agent/tool_loop_adapter.py`）：模型每轮从固定工具表里挑带类型的工具，没有任何写操作；
- 自然语言创建、多轮对话重跑整段循环、提问不丢已搜到的方案、结构化表单降级；
- 中英文城市别名确定性标准化，以及无可行方案的具体库存诊断；
- 库存快照与交接链接有效期校验，过期数据不得进入安全交接；
- 可选 PostgreSQL 任务仓储、Alembic 迁移、乐观锁和重启恢复；
- Provider 原始响应写入内存或本地 WORM 对象存储，保存哈希、保留期和受限读取策略；
- Duffel Test Mode 测试订单支持一次性创建、读取、取消和取消后复查；拒绝 Live token、真实旅客邮箱和写操作重试；
- 可选登录认证、短期签名 Bearer 会话，以及员工/直属审批人/管理员资源级授权；
- 外部 JSON 政策配置、启动时严格校验、城市代码目录和历史政策快照固定；
- 脱敏 Replay 数据集、版本化清单、文件哈希、路径安全和整库去重校验；
- 每任务统一 12 次工具调用预算，覆盖 LLM、库存查询、重验和交接端口；
- 员工习惯画像：从**他真的订过的行程**里推出常坐高铁、躲早班、住得离客户近这类习惯，每条都带得出理由；只改排序，不碰可行性和政策结论；**默认关闭**（不传历史来源即与此前逐字一致）；
- 选项上的代价说明：超出差标多少、该谁批、换哪条能省多少及其代价，全部由确定性代码从**已经查到的方案**里算，不额外调供应商、不消耗工具预算；
- 业务结果指标层：交接完成率、平均耗时、提前预订天数和超标发生率，只读任务聚合与审计事件，不调模型和供应商；管理员端点 `GET /metrics/business`；
- 产品入口可离线评测（D16）：`DeterministicToolCallingModel` 让 60 条工作流和 480 条意图冻结用例经工具循环逐条执行，并与语义入口并排；`tests/test_product_entrypoint_evaluation.py` 是 CI 门禁；
- 工具循环的交付契约：`propose_options` 声明旅行者的硬要求与偏好（此前一条都不到规划器）；搜空落 `NO_FEASIBLE_OPTION`，越界落 `OUT_OF_SCOPE`，都不占澄清轮数；
- 管理端看板：管理员默认视图，三块只读——业务结果指标（`GET /metrics/business`）、谁在哪（`GET /duty-of-care`，只来自确认过的行程）、成本中心预算消耗（`GET /budgets`，区分已确认支出和已交接未回填的在途金额）；
- 一趟差旅与改期：`Trip` 聚合跨越规划任务和改期任务；下单确认后登记观察对象，航变/会议改期事件（`POST /trips/{id}/events`，会议改期可指定 `leg_index`）开一个新的改期任务挂在同一趟差旅下，原任务不动；指标多了变更场景人工介入率；
- 订好之后的追踪（2026-09-02）：**被动**——watch worker 从起飞前 48 小时起按节奏问航班动态源（`FLIGHT_STATUS_SOURCE`），或企业侧 HMAC 推送到 `POST /flight-status/webhook`；每条动态先过**确定性影响评估**（`services/change_impact.py`）：取消 / 晚于"到场时限 − 安全缓冲" / 接不上下一段才开改期任务，延误但来得及只通知（发件箱 `TRIP_FLIGHT_STATUS_NOTICE`），同样的动态不重复通知。**主动**——已订任务的聊天继续可用，模型把"会议改到周五" / "航司说取消了" / "这趟不去了"读成一条变更请求（日期必须引用旅行者原话），落到同一个事件接口；`POST /trips/{id}/cancel` 取消整趟差旅，观察停止、看板不显示，退改票由人去办；
- 费控对账：导入费控记录（`POST /expenses/import`），按旅行者 + 订单号和下单确认匹配；对上了自述才算核实过，找不到确认的就是渠道外预订——`off_channel_expense_rate` 第一次有真值；预算账本对过账的用费控的数；
- 代订：任务记旅行者和发起人两个人，谁能替谁订由员工档案的委托名单决定（演示：助理 `A1002` 可替 `E1001` 订）；差标、审批、预算全看旅行者，助理和高管都看得见、都能操作；
- 分级审批与事务性发件箱：审批链由政策 `approval_tiers` 决定（金额过档或触发某条规则就多一级），收件箱按"当前待谁批"查；审批各步与下单确认随任务更新同一事务进 `outbox_events`，`examples/run_outbox_worker.py` 或 `POST /outbox/dispatch` 投递到日志 / webhook（HMAC 签名）/ 仓库内模拟的外部审批系统；
- 政策四个维度：夜费基础上限之外，淡旺季上限（按城市 × 日期窗口）、提前预订天数、成本中心预算余额；预算的"用掉多少"来自员工回填的下单确认，规划时钉成 `BudgetSnapshot`，账本没接就判"判不了"而不是当作没超；
- 下单确认回流：交接之后员工回填订单号和实付金额（`POST /trip-tasks/{id}/booking-confirmation`），任务进入 `BOOKING_CONFIRMED`；指标层由此得到确认预订率、真实下单时刻的提前预订天数和实付偏差。**是自述不是回执**——系统核不了订单号，来源固定标为 `SELF_REPORTED`；一个任务一条，写了不改；
- FastAPI 外壳以及 887 个测试；另有 540 条版本化派生评测案例：60 条逐条运行完整工作流，480 条逐条运行真实意图编排入口并按来源、场景和 cohort 输出质量与安全指标。

## 架构

```mermaid
flowchart TD
    API["FastAPI / 调用方"] --> ORCH["Trip Workflow Orchestrator"]
    API --> AUTH["Authentication / Resource Authorization"]
    ORCH --> LLM["Schema-bound LLM Port"]
    ORCH --> DIRECTORY["Employee Directory"]
    DIRECTORY --> CONFIG["Validated Policy Configuration"]
    ORCH --> PROVIDER["TravelInventoryProvider"]
    ORCH --> BUDGET["Task Tool Budget / 12 calls"]
    PROVIDER --> MOCK["Mock Provider"]
    PROVIDER --> REPLAY["Replay Provider"]
    PROVIDER --> DUFFEL["Duffel Test Mode Flights"]
    PROVIDER --> RAW["Immutable Raw Response Store"]
    ORCH --> PLANNER["Itinerary Planner"]
    PLANNER --> VALIDATOR["Feasibility Validator"]
    PLANNER --> POLICY["Deterministic Policy Engine"]
    ORCH --> STATE["State Machine"]
    ORCH --> AUDIT["Task / Audit Repository"]
```

LLM 端口只允许做意图抽取、受控查询调整和事实解释。它不能输出政策批准结论，也不能构造库存 ID。自然语言输出先经过严格 Pydantic schema，再由应用层重新检查必填字段、时区、时间顺序和硬约束冲突。结构化输出实现依据 [OpenAI Structured Outputs 官方指南](https://developers.openai.com/api/docs/guides/structured-outputs)。

LLM 与 Provider 端口共享任务级调用预算。调用前先预留名额，失败调用同样计数；达到 12 次后进入 `TOOL_BUDGET_EXHAUSTED`，不会再触发外部调用或创建交接意图。API 的 `tool_budget` 字段返回上限、已用、剩余和有序调用记录。

更完整的边界、状态和不变量见 [docs/architecture.md](docs/architecture.md)，需求覆盖情况见 [docs/requirements-traceability.md](docs/requirements-traceability.md)。

## 快速运行

核心演示和测试依赖 Python 3.11+ 与项目声明的 Pydantic 依赖：

```bash
cd corporate-travel-agent
PYTHONPATH=src python3 examples/run_demo.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

启动 API 和可选 LLM 适配器：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,llm,persistence]'
export OPENAI_API_KEY='your-server-side-key'
export OPENAI_MODEL='your-actual-model-id'
uvicorn corporate_travel_agent.api.main:app --reload
```

然后访问 `http://127.0.0.1:8000/docs`。内置 Mock 数据使用员工 `E1001`，覆盖北京到上海的推荐演示场景。

自然语言创建任务：

```bash
curl -X POST http://127.0.0.1:8000/agentic/trip-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "traveler_id": "E1001",
    "message": "我下周三从北京去上海，周四上午十点开会，下午回来。不要太早，酒店离客户近一点，能坐高铁最好。"
  }'
```

如果状态为 `NEEDS_CLARIFICATION`，继续调用：

```bash
curl -X POST http://127.0.0.1:8000/agentic/trip-tasks/TASK_ID/messages \
  -H 'Content-Type: application/json' \
  -d '{"message": "最晚需要周四上午九点到客户公司"}'
```

未配置 `OPENAI_API_KEY` 时，自然语言入口返回 `503`，结构化创建和确定性核心仍可使用。服务端不会把 API Key 写入 Prompt、响应或审计事件。

自然语言产品入口是工具循环：新任务使用 `POST /agentic/trip-tasks`，后续消息使用
`POST /agentic/trip-tasks/{task_id}/messages`。模型每轮决定查什么，没有一张必填表挡在
搜索前面。曾经并行的 `/semantic/trip-tasks` 和 `/legacy/trip-tasks` 已于 2026-09-01 删除
（ADR-0003），
结构化创建仍用 `POST /trip-tasks`。每个任务固定返回 `intent_entrypoint`，不同入口之间不能
交叉继续。

## Duffel Test Mode 航班与 LiteAPI 酒店接入

V1 可选接入 Duffel Test Mode 的航班搜索与 Offer 重新验证。它调用真实 Duffel
HTTPS API，但返回沙箱库存；不会创建 Order、Payment 或真实出票。LiteAPI
（现品牌为 Nuitee Connect）可用于酒店报价搜索。组合模式下，酒店重验只会再次调用
`POST /hotels/rates`；项目不会调用 LiteAPI `prebook`、`book`、支付、取消或客人资料端点。

先在 Duffel Dashboard 的 Developer test mode 创建 `duffel_test_` 令牌，然后在同一
服务进程中配置：

```bash
export TRAVEL_PROVIDER=duffel
export DUFFEL_ACCESS_TOKEN='duffel_test_...'
export DUFFEL_API_VERSION=v2
export DUFFEL_LIVE_MODE=false
export LIVE_BOOKING_ENABLED=false
```

同时连接 Duffel 航班和 LiteAPI 酒店时，从 LiteAPI/Nuitee Connect Dashboard
取得 API Key，然后配置：

```bash
export TRAVEL_PROVIDER=duffel_liteapi
export LITEAPI_API_KEY='sand_...'
export LITEAPI_REQUIRE_SANDBOX=true
export LITEAPI_CURRENCY=CNY
export LITEAPI_GUEST_NATIONALITY=CN
export LITEAPI_BOOKING_ENABLED=false
export LIVE_BOOKING_ENABLED=false
```

默认支持北京、上海、伦敦、纽约以及直接传入的三字母 IATA 代码。其他地点必须用
`LITEAPI_LOCATIONS_JSON` 配置经核验的位置，例如：

```bash
export LITEAPI_LOCATIONS_JSON='{
  "Shenzhen":{"cityName":"Shenzhen","countryCode":"CN"},
  "Paris":{"iataCode":"CDG"}
}'
```

LiteAPI 搜索返回的是整段住宿价格，适配器会换算成每晚价格供政策引擎判定。
LiteAPI 不知道客户办公室地址，因此通勤时间会明确标记为未知（`1440`），
不会伪造“距客户 10 分钟”之类证据。

先运行一次只读真实连通测试（默认查询 30 天后的上海一晚）：

```bash
set -a && . ./.env && set +a
.venv/bin/python examples/run_liteapi_hotel_smoke.py \
  --output reports/evaluation-runs/liteapi-hotel-smoke \
  --confirm-external-test-call
```

该脚本最多发起一次 `/hotels/rates` 请求，并检查返回报价、Sandbox 声明、
原始响应存档、API Key 未泄露和禁止预订/支付边界。LiteAPI 官方请求字段与返回
结构见 [Hotel rates API](https://docs.liteapi.travel/reference/post_hotels-rates) 和
[Rates JSON guide](https://docs.liteapi.travel/docs/hotel-rates-api-json-data-structure)。

航班+酒店完整只读链路使用冻结案例
`evals/subsets/duffel-liteapi-real-full-chain-v2.json`：

```bash
set -a && . ./.env && set +a
.venv/bin/python examples/run_duffel_liteapi_full_chain_smoke.py \
  --output reports/evaluation-runs/<run-id> \
  --confirm-external-test-calls
```

该执行器串联 Duffel 航班搜索、LiteAPI 酒店搜索、组合方案、用户选择、两侧报价重验和
只读交接。无故障时基线为 4 个外部请求；对明确分类为瞬时故障的只读操作，系统默认
最多尝试 3 次，因此最坏请求预算为 12。评测分别记录业务调用与重试调用，只要求总量
不超过预算，不要求严格等于上限；任何预订或支付端点仍然禁止调用。

北京和上海默认映射到 Duffel IATA 位置代码 `BJS`、`SHA`。其他非 IATA 城市名必须通过
`DUFFEL_LOCATION_CODES_JSON` 提供经核验的映射，例如
`'{"London":"LON","New York":"NYC"}'`；无法核验的名称会在发请求前失败，避免参数幻觉。

Duffel 报价币种必须与企业政策币种一致，否则政策引擎会因缺少受信任 FX 快照而拒绝
组合报价。可用 `DUFFEL_EXPECTED_CURRENCY` 在 Provider 边界提前拒绝不一致币种。
`GET /health` 会公开 `travel_provider` 与 `travel_provider_mode`，但不会公开令牌。

**真实模型 × 真实 Duffel 的旧评测线路（D9–D12：资格预检、组合冒烟、连接故障恢复回归、
重评分）已于 2026-09-01 随 legacy 入口一起删除**（`docs/adr/0003-single-product-entrypoint.md`）。
它们驱动的是旧的意图抽取入口；历史报告仍在 `reports/evaluation-runs/`（`d4-model-*`、
`phase10-duffel-real-workflow-*` 等）留档，但不再可复跑。产品入口（工具循环）下的真实模型
评测由 `examples/run_tool_loop_calendar_evaluation.py`、`run_tool_loop_multicity_evaluation.py`、
`run_tool_loop_live_multiturn_evaluation.py` 和 `run_agentic_boundary_longtail_evaluation.py`
产出（`reports/evaluation-runs/toolloop-*`、`agentic-boundary-*`）。自 2026-09-01 起它们统一经
`evaluation/tool_loop.py` 记账：每个报告目录里都有协议 §5 的 `traces.jsonl`、逐次计价的
`cost-ledger.jsonl`、`cost-summary.json` 和 `retry-cap-check.json`（LLM 最多 2 次、供应商最多 3 次，
402 之后绝不重试）。对抗集（库存文本注入、编造引用、写工具、身份替换、越权审批/交接）在
`examples/run_adversarial_tool_loop.py`，0 计费、0 外部调用，也是 CI 门禁
（`tests/test_adversarial_tool_loop.py`）。

外部真实用户原话（D18/D19，`data/evaluation/external-longtail-v1`：ChinaTravel 154 + CrossWOZ 100 +
AirDialogue 46）：单轮红线探针 `examples/run_external_longtail_probe.py`（离线 $0）和
`run_external_longtail_live_sample.py`（真模型），2026-09-02 真模型三轮 300 条 pass^3 99.0%；
多轮续问 `run_external_longtail_live_multiturn.py` 给每条配事实表由脚本化模拟旅行者逐轮交出，
量"能不能办成"：185 条三轮 completion^3 84.0%，并产出盲评输入 `judge-inputs.jsonl`
（`reports/evaluation-runs/external-longtail-*-3x-20260902/`，协议 §3.3）。
三轮重跑的**一致性**另算（比决策不比文本：终局决策 / 搜索参数 / 声明的硬要求 / 追问目标四层，外加不设门禁的
过程离散度）：`examples/run_consistency_evaluation.py` 从现有报告零计费算出。2026-09-06 带第 3 层的 185 × 3
重跑：终局决策一致 174/185、搜索参数去重后一致 138/143、**声明的硬要求一致 129/137**（偏好只有 85/137，只报告）、
追问核心事实一致 136/179；completion^3 86.0%；三轮裁判均分 4.07 / 3.92 / 4.00（未校准）。终局摇摆 11 条里 6 条
根因是"必须高铁"要不要声明成硬约束，规则待定（`reports/evaluation-runs/consistency-external-longtail-multiturn-3x-v2-20260906/`，
协议 §6.7，HANDOFF §59）。

D13 继续覆盖真实 Provider 的选择与报价重验路径。它固定执行一次 Duffel Test Mode
搜索，再对一个合规方案执行一次 `GET /air/offers/{offer_id}`；允许报价保持不变，或在
涨价时安全进入重新确认，但始终禁止 Order 与 Payment：

```bash
.venv/bin/python examples/run_duffel_provider_workflow_smoke.py \
  --dataset evals/subsets/duffel-real-revalidation-smoke-v1.json \
  --confirm-external-test-call \
  --output reports/evaluation-runs/<duffel-revalidation-run-id>
```

2026-08-09 的 D13 真实运行 19/19 门禁通过：2 次外部请求严格为 Offer Request POST 与
Offer GET，42 个标准化报价、3 个方案；选中报价重验为 `UNCHANGED`，工作流进入
`READY_FOR_HANDOFF`。生成的 `BookingIntent` 只是本地幂等交接状态，说明文本明确写明
Test Mode 且未创建订单；Duffel Order 与 Payment 调用均为 0。搜索与重验原始响应均以
0600 权限归档并记录 SHA-256。结果位于
`reports/evaluation-runs/duffel-revalidation-smoke-20260809/`。

同一冻结案例的 1×3 Provider 稳定性回归使用独立聚合 runner，总调用上限为 6：

```bash
.venv/bin/python examples/run_duffel_revalidation_stability.py \
  --dataset evals/subsets/duffel-real-revalidation-smoke-v1.json \
  --attempts 3 \
  --confirm-external-test-calls \
  --output reports/evaluation-runs/<duffel-revalidation-stability-run-id>
```

2026-08-09 的 D13 1×3 回归为 3/3、`pass^3=1`、mixed=0，12/12 聚合门禁通过。
实际请求 6/6，三轮均为 `UNCHANGED` 且工具轨迹一致，搜索/重验原始响应各归档 3 份，
敏感标记为 0，Order/Payment 为 0。三次独立搜索选中报价分别为 USD 215.70、215.62、
223.10；这是不同 Offer Request 的 Test Mode 库存差异，每一轮搜索后紧接的 Offer 重验
价格均未变化。结果位于
`reports/evaluation-runs/duffel-revalidation-stability-20260809/`。

D14（真实模型 + Duffel Test Order 创建/读取/取消/复查的端到端冒烟）的 runner 同样随 legacy
入口删除。Test Order 那一层的能力和门禁仍在 `providers/duffel_test_order.py`，由
`tests/test_duffel_test_order.py` 守着：拒绝 Live token、真实旅客邮箱和写操作重试。要再做一次
带真实模型的 Test Order 冒烟，得先给工具循环写一个等价 runner。

## Provider 熔断与延迟重试

只读 Provider 调用默认最多执行 3 次即时尝试（首次调用加最多 2 次重试）。瞬时错误仍未
恢复时，系统打开 60 秒熔断器并把任务持久化为 `WAITING_FOR_PROVIDER`；请求线程立即返回，
后台任务按 60 秒、3 分钟、10 分钟的默认计划最多再尝试 3 次。服务重启后会从任务元数据
恢复待执行时间。任一延迟尝试恢复后，任务继续原来的搜索或重验流程；全部耗尽后进入
`PROVIDER_FAILED`，保留失败、次数和审计证据。订单、支付、取消等外部写操作不进入此自动
重试链路。

可通过环境变量调整等待计划，但延迟重试上限不能超过 3：

```bash
export PROVIDER_CIRCUIT_OPEN_SECONDS=60
export MAX_DELAYED_PROVIDER_RETRIES=3
export PROVIDER_DELAYED_RETRY_SECONDS=60,180,600
export PROVIDER_RETRY_POLL_SECONDS=5
export PROCESS_ROLE=worker
export PROVIDER_RETRY_WORKER_ID=retry-worker-1
export PROVIDER_RETRY_LEASE_SECONDS=900
```

Web API 进程默认 `PROCESS_ROLE=api`，不会启动重试消费者；应以独立 worker 进程运行
`PROCESS_ROLE=worker`。到期任务先在数据库中原子 claim，并写入 `lease_owner`、
`lease_until` 和唯一 fencing token；另一个 worker 在 lease 有效期内不能重复调用 Provider，
lease 过期后可以接管，旧 worker 的迟到写回会被 token 条件拒绝。默认 15 分钟 lease 覆盖
当前有界只读调用的最坏执行时间；关闭时先停止 claim 并等待在途调用 drain。

任务 API 的 `provider_retry` 字段公开当前状态、已完成次数、上限、下一次执行时间和熔断器
快照，不公开供应商响应正文或凭证。所有重试继续消耗原任务的 12 次工具预算，因此预算耗尽
可能早于第 3 次延迟重试安全终止。

Provider 报价重验上下文会写入 `ProviderQuoteContextStore`。启用 PostgreSQL 时，Duffel
Offer ID、LiteAPI 查询/房型签名、原始价格/币种和供应商有效期会持久化，使搜索后的选择请求
可以由另一 worker 或重启后的进程继续处理；API key、认证头和访问令牌会被拒绝写入。这里
持久化的是“如何重验”的最小上下文，不是把航班报价当成持续有效的业务缓存：选择或批准后
仍会实时调用供应商，`expires_at` 到期则以稳定错误码拒绝并要求重新搜索。过期记录可按 TTL
清理，内存实现另有容量上限。

## 订好之后：航班动态、影响评估与变更

下单确认（`BOOKING_CONFIRMED`）之后，差旅进入观察：系统盯着确认方案的每一段，盯到最后一段
落地后一天。变更有两个来源，落到同一个事件接口，改期任务照常走规划、政策、审批、交接。

**被动（航班取消、延误）。** watch worker 从最早一段起飞前 48 小时起按节奏问航班动态源
（48–12 小时每 6 小时，12–3 小时每小时，3 小时内每 15 分钟，起飞后每 30 分钟），或者企业侧
把推送 POST 过来。每条动态先过确定性影响评估，**不是收到就改期**：

| 动态 | 判定 | 系统做什么 |
|---|---|---|
| 取消 | `REBOOK_REQUIRED` | 开改期任务，被取消的票不再端上来，事件带评估依据 |
| 延误，新到达晚于"到场时限 − 安全缓冲"，或接不上下一段 | `REBOOK_REQUIRED` | 同上 |
| 延误 ≥ 15 分钟但仍来得及 | `NOTIFY_ONLY` | 发件箱 `TRIP_FLIGHT_STATUS_NOTICE`，行程不动 |
| 按计划 / 变化很小 / 查不到 | `NO_CHANGE` | 只记观察 |

```bash
export FLIGHT_STATUS_SOURCE=memory          # none（默认，不查）| memory（进程内表，接推送 / 演示）
export TRIP_WATCH_POLL_SECONDS=60
export TRIP_WATCH_LEASE_SECONDS=300
export TRIP_WATCH_LOOKAHEAD_HOURS=48
export TRIP_CHANGE_MIN_CONNECTION_MINUTES=60
export FLIGHT_DELAY_NOTICE_MINUTES=15
export FLIGHT_STATUS_WEBHOOK_SECRET='shared-with-the-tmc'   # 不配就不收推送（503）
.venv/bin/alembic upgrade head                               # 迁移 0012：trips.next_check_at + 租约
PROCESS_ROLE=worker .venv/bin/python examples/run_trip_watch_worker.py --interval 60
```

推送：`POST /flight-status/webhook`，请求体 `{"ref_id":"MU-EARLY","status":"DELAYED",
"estimated_arrive_at":"2026-08-05T09:05:00+08:00","source":"tmc"}`，头
`X-Flight-Status-Signature` 是对原始请求体的 HMAC-SHA256；不带 `trip_id` 时对每一趟盯着这张票的
差旅都算数（同一班航班上可能坐着好几位旅行者）。管理员也可以直接 `POST /trips/{id}/flight-status`；
`POST /trip-watch/run` 手动跑一轮。真实动态源（VariFlight、AeroDataBox、TMC 查询接口）实现
`providers/flight_status.FlightStatusPort.status_of` 即可，仓库里不放没有凭证就验不了的适配器。

评测：`examples/run_trip_change_evaluation.py`（协议 D20）把上面每一格各写成 ≥10 条用例，
`--offline` 只跑确定性的 11 类（$0），不带 `--offline` 再跑 4 类真模型聊天变更；`--runs 3` 出
"每轮都过"的条数。2026-09-02 首跑 15 类 × 10 条 × 3 轮 450/450
（`reports/evaluation-runs/trip-change-eval-20260902/`）。离线走一遍完整链路看
`examples/run_trip_watch_walkthrough.py`。

**主动（会议改期、不去了）。** 已订任务的聊天继续可用：`POST /agentic/trip-tasks/{id}/messages`
在 `BOOKING_CONFIRMED` 上不重跑规划，而是让模型把话读成一条变更请求——会议改期（哪一段、新的
最晚到达时刻，**日期必须逐字引用旅行者原话**）、航变自述（指到已订行程里的票号）、取消。读不出
来就问一句，任务状态不变。表单路径：`POST /trips/{id}/events`（`event_type=MEETING_MOVED`，
可带 `leg_index`）和 `POST /trips/{id}/cancel`。取消之后观察停止、谁在哪看板不再显示、发件箱
`TRIP_CANCELLED`；**退票、改签仍由人去官方平台办**，系统记不到退改费。

## PostgreSQL 持久化

不设置 `DATABASE_URL` 时仍使用内存仓储。启用本地 PostgreSQL：

```bash
docker compose up -d postgres
export DATABASE_URL='postgresql+psycopg://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent'
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn corporate_travel_agent.api.main:app --reload
```

`GET /health` 中的 `persistence` 应为 `postgresql`，并包含 `persistence_details`（池与 Alembic 版本）与 `outbox`。迁移后可以运行重启恢复冒烟：

```bash
.venv/bin/python examples/verify_postgres.py
```

脚本会创建一个 Mock 任务，关闭并重新建立仓储连接，然后继续选择方案，再次重连确认 Booking Intent、审计事件和库存快照仍然存在。真实 Provider 的重验上下文存放在
`provider_quote_contexts`，迁移 `0005_provider_quote_contexts` 创建该表和查询/TTL 索引。开发数据保存在 Compose 命名卷中；`docker compose stop postgres` 只停止服务，不删除数据。

`DATABASE_AUTO_CREATE=true` 仅适合临时开发环境；`ENVIRONMENT=production|staging` 时会被拒绝。正常环境应使用 Alembic 迁移。

载荷带 `schema_version`（当前 3），旧版本读取时自动升级；批量改写用
`examples/upgrade_task_payloads.py`（先 `pg_dump`）。只有 Postgres 才有的行为（`SKIP LOCKED`、
JSONB、联合写入）由 `tests/test_postgres_live.py` 在 `TEST_DATABASE_URL` 指向一次性测试库时覆盖。
分阶段能力与运维说明见 **[docs/postgres-operations.md](docs/postgres-operations.md)**：

| 阶段 | 内容 |
|---|---|
| A | 连接池、健康检查、备份手册、CI |
| B | 任务投影、摘要列表、`/approvals/inbox`、按索引的延迟重试 |
| C | 政策配置入库、`import_policy_config.py` |
| D | Outbox 表与未投递计数 |

## Provider 原始响应归档

未设置 `RAW_RESPONSE_STORE_DIR` 时使用进程内存后端，适合单元测试。内部试用应启用本地 WORM 后端：

```bash
export RAW_RESPONSE_STORE_DIR="$PWD/data/runtime/raw-provider-responses"
export RAW_RESPONSE_RETENTION_DAYS=90
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn corporate_travel_agent.api.main:app --reload
```

`GET /health` 中的 `raw_response_store` 应为 `local-worm`。每次 Provider 查询都会先保存规范化前的 JSON 正文，再把 SHA-256、大小、内容类型、保留截止时间、访问策略和内部对象键绑定到库存快照。对象正文与元数据均以只写一次方式创建；相同键只能幂等写入相同内容。

库存快照 API 只公开归档状态、哈希、大小和保留期，不返回内部对象键或正文。正文读取仅允许系统回放角色或审计管理员上下文；当前 HTTP 层没有正文下载路由。正式部署仍应把同一 `RawResponseObjectStore` 接口替换为启用 Object Lock 的 S3 兼容存储。

可运行不调用模型的端到端归档冒烟：

```bash
.venv/bin/python examples/verify_raw_response_storage.py
```

## 脱敏 Replay 回归库

已经持久化的任务可安全导出为写入一次的 Replay 数据集。导出器会读取受限 WORM 原文，执行 `travel-redaction-v1` 脱敏，移除标准化快照中的内部对象引用，并为查询、快照和脱敏原文建立严格清单与 SHA-256。

验证当前 Mock 管道样例：

```bash
.venv/bin/python examples/validate_replay_dataset.py \
  data/replay-datasets/mock-smoke-20260801
.venv/bin/python examples/validate_replay_library.py data/replay-datasets
```

当前样例包含 3 个 Mock 快照，不能计入 20 个真实快照目标。整库校验只统计 `AUTHORIZED_API` 与 `BROWSER_ASSISTED` 来源，并拒绝重复源响应。详细采集和导出规则见 [docs/replay-datasets.md](docs/replay-datasets.md)。

离线派生评测集位于 `data/evaluation/derived-v2`，包含 60 条 PreferTripPlan 派生工作流案例和 480 条跨数据集意图案例。意图集完整纳入 PreferTripPlan 的 225 条 curated test 和 Open-Travel 的 250 条分类 test，并保留 5 条旧版训练案例用于连续对比；默认通过确定性解析器逐条进入 `create_task_from_message()`，真实模型可通过同一评测接口替换。所有库存均为 `MOCK`。运行：

```bash
.venv/bin/python examples/validate_evaluation_dataset.py data/evaluation/derived-v2
```

许可、转换边界和重建方式见 [docs/derived-evaluation-dataset.md](docs/derived-evaluation-dataset.md)。

## 企业政策配置

可编辑的配置样例位于 [`config/travel-policy.json`](config/travel-policy.json)，覆盖以下数据：

- 规范城市代码、Provider 名称和中英文别名；
- 审批人目录，以及每位员工的职级、部门、常驻城市和直属审批人；
- 各职级允许的机票/火车席别、分城市酒店上限和到达缓冲；
- 允许发起例外审批的规则，以及当前生效的政策快照。

编辑后先独立校验，不会启动服务或调用模型：

```bash
.venv/bin/python examples/validate_policy_config.py config/travel-policy.json
export POLICY_CONFIG_FILE="$PWD/config/travel-policy.json"
```

任何未知字段、重复/冲突城市别名、未知城市或审批人引用、非法金额、未知例外规则、无效时区和缺失职级规则都会使服务拒绝启动。`GET /health` 应显示：

```json
{
  "policy_config": "external",
  "policy_config_version": "company-travel-config-v1",
  "active_policy_snapshot": "policy-travel-v1-20260801"
}
```

升级政策时应在 `policies` 中追加新的 `snapshot_id` 和 `policy_version`，再修改 `active_policy_snapshot_id`；不要删除或改写仍被历史任务引用的快照。系统会把政策内容哈希固定到新任务，同一快照 ID 被改写后，旧任务会安全失败。员工资料变化也应递增 `profile_version` 并更换 `snapshot_id`。

## 登录与资源级授权

`AUTH_ENABLED=false` 仅用于本机开发兼容。内部试用前，运行交互式工具；它会读取 `POLICY_CONFIG_FILE`，为其中的全部员工、审批人和本地管理员 `A9001` 设置密码：

```bash
.venv/bin/python examples/create_auth_config.py
source data/runtime/auth.env
```

工具只保存 scrypt 密码哈希，凭证文件与签名密钥文件权限均为 `0600`，不会把明文密码写入磁盘。然后在同一终端设置数据库和归档变量并重启 Uvicorn。`GET /health` 的 `authentication` 应为 `enabled`。

登录获取一小时有效的短期 Bearer 令牌：

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"E1001","password":"你的密码"}'
```

后续请求增加 `Authorization: Bearer ACCESS_TOKEN`。权限边界如下：

| 身份 | 读取范围 | 可修改任务 | 可审批 |
|---|---|---|---|
| 员工 | 本人任务 | 仅本人任务 | 否 |
| 审批人 | `manager_id` 指向本人的直属任务 | 否 | 仅本人审批范围 |
| 管理员 | 全部任务 | 全部任务 | 不可冒充直属经理 |

启用认证后，审批人身份来自 Bearer 令牌；请求体中的不一致 `approver_id` 会被拒绝。越权读取返回与不存在任务相同的 `404`，避免通过任务 ID 枚举资源。正式生产环境仍应接企业 OIDC/SSO、集中会话吊销与网关级登录限流。

## 目录

```text
src/corporate_travel_agent/
  agent/        # 有界编排器和 LLM 端口
  api/          # FastAPI 传输层
  domain/       # 不依赖框架的不可变领域模型
  planning/     # 组合、可行性、成本与排序
  policy/       # 确定性政策引擎
  providers/    # Provider 协议、Mock、Replay
  services/     # 目录、仓储和审计服务
  workflow/     # 显式状态机
tests/          # 政策证据与端到端状态测试
examples/       # 可直接运行的演示
docs/           # 架构、需求追踪和路线图
config/         # 外部企业政策配置样例
```

## 当前阶段

当前已经完成“领域核心 + 自然语言/澄清 + 规划闭环 + 审批/版本 + PostgreSQL 持久化 + 原始响应归档 + 内部试用授权 + 外部政策配置”的框架基线。达到完整 V1 Definition of Done 仍需完成完整真实模型评估、企业 OIDC/SSO 和 20+ 回放快照；详见 [docs/roadmap.md](docs/roadmap.md)。

按产品设计思路对照出的缺口和改进优先级，见 [docs/product-gap-review.md](docs/product-gap-review.md)。
