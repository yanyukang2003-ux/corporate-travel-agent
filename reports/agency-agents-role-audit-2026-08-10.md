# Agency-Agents 多角色审计报告

**日期：** 2026-08-10  
**项目：** `corporate-travel-agent`  
**工作区：** `/Users/yukangyan/Downloads/corporate-travel-agent`  
**方法：** 基于 [agency-agents](https://github.com/msitarzewski/agency-agents) 筛选出的 6 个核心角色独立审查，再去重合并。  
**原始角色产出：** `reports/_audit-wip/{workflow,backend,ai,api,sre}.md`（Codex 会话完成）；Security Architect 在 Codex 侧因额度中断，本报告中由本地只读复核补全并交叉验证。

---

## 1. 审计范围与证据标准

### 1.1 角色范围

| 角色 | 责任域 | 状态 |
|---|---|---|
| Workflow Architect | 状态机、分支、恢复、审批/重验/交接契约 | 完成 |
| Backend Architect | FastAPI、持久化、幂等、事务、Provider 契约 | 完成 |
| AI Engineer | 意图抽取、参数环、评测门禁、模型适配/成本 | 完成 |
| API Tester | HTTP 契约、供应商错误分类、鉴权、测试隔离 | 完成 |
| SRE | 健康检查、SLO/可观测性、多 worker、部署/恢复 | 完成 |
| Security Architect | 鉴权 fail-open、PII/审计、外部副作用、评测安全断言 | 补全完成 |

### 1.2 证据标准

- **只读审查为主**：不修改业务代码；不调用真实外部供应商；不创建 Test Order。
- **每条 finding** 需包含：严重级别、失败场景、证据路径（文件/行号或本地复现）、修复建议、验证方式。
- **交叉确认优先**：同一根因被多个角色独立指出时合并为一条，并标注来源角色。
- **区分两类结论：**
  1. **内部试用是否可接受**（单进程、可监督、PostgreSQL、只读供应商）
  2. **公网/多实例生产是否可发布**（默认配置安全、可观测、可回滚、无假阳性门禁）
- **不在范围内：** 正式支付/出票、OIDC/SSO 企业身份体系、前端 UI、多 Agent 编排改造。

### 1.3 当前基线（项目事实）

摘自 `HANDOFF.md` 与仓库现状（审计日）：

| 项 | 状态 |
|---|---|
| D4 agent-eval frozen | v1.0.2 |
| DeepSeek smoke / 稳定性 | 24/24、24×3 全过（workflow 断言维度） |
| Duffel 重验 / Test Order | 重验 19/19；D14 正式 28/28 PASS（订单已取消） |
| Provider 韧性 | 3 次即时尝试 + 60s 熔断 + 最多 3 次延迟重试 + `WAITING_FOR_PROVIDER` |
| 明确未验收 | 真实上游延迟恢复；LLM 失败优先澄清；OOS 收紧/重开 |
| 仓库元数据 | **无 `.git`、无 `.github`/CI**；`compose.yaml` 仅开发 PostgreSQL |

---

## 2. 总评

| 场景 | 结论 | 说明 |
|---|---|---|
| 本地开发 / 单进程受控演示 | **可用** | 确定性状态机、政策引擎、Provider 边界、评测体系扎实 |
| 网络可达的内部试点（默认配置） | **阻塞** | 鉴权 fail-open 为管理员；缺 `DATABASE_URL`/`RAW_RESPONSE_STORE_DIR` 时静默用内存；`/health` 恒 `ok` |
| 公网 / 多实例生产 | **不可发布** | 无 CI/部署/回滚证据；跨 worker 报价缓存与熔断不共享；可观测性与 SLO 缺失 |
| 评测“安全/溯源”门禁可信度 | **部分失效** | D4 的 unsupported-parameter / untrusted-mutation 观测值被硬编码为 0 |

**一句话：** 主业务路径与确定性内核质量高，但“默认配置能否安全上线”和“评测是否真正量了安全”仍是关键缺口；HANDOFF 列出的 Step2/OOS/真实延迟恢复三项均为实锤问题，不是文档空话。

---

## 3. 跨角色共同优势

1. **单 Agent + 确定性内核边界清晰**：LLM 不能直接选工具、批政策或写库存；政策/规划在确定性代码中。
2. **状态机集中审计**：`StateMachine.transition` 管控合法边；审批不能直接跳到 handoff。
3. **Provider 写操作隔离**：Duffel 拒绝非 test token / live booking；LiteAPI 只读 rates；Test Order 需显式授权且不进自动重试。
4. **只读重试与熔断已落地**：即时 3 次、延迟最多 3 次、审计字段完整、SQL 乐观锁 + 任务审计同事务。
5. **原始响应对象存储设计较好**（启用目录时）：写一次、SHA-256、访问策略；接口层不可变。
6. **评测资产冻结与指纹**、多语言/对抗切片、成本 ledger 体系成熟（但安全断言见 C2）。

---

## 4. 合并 Findings（去重后）

严重级别定义：

- **Critical**：默认部署可造成未授权全权访问、数据静默丢失，或发布门禁给出假阳性安全结论。
- **High**：真实故障下任务不可恢复/跨实例失败/线程池打满/重要契约错误，试点前应修。
- **Medium**：可运维性、契约完整性、证据完整性、体验退化。
- **Low**：文档漂移、边界打磨、资源关闭等。

### Critical

#### C1 — 运行时配置 fail-open：无鉴权管理员 + 可挥发持久化

| 字段 | 内容 |
|---|---|
| **来源** | Backend, API Tester, SRE, Security（补全） |
| **证据** | `services/auth.py:97-105,146-152`：`AUTH_ENABLED` 默认 false，返回 `development-system` + `Role.ADMIN`；`api/main.py:79-104`：缺 `DATABASE_URL`/`RAW_RESPONSE_STORE_DIR` 用内存；`.env.example` 示例 `AUTH_ENABLED=false`；审批在 auth 关闭时跳过身份绑定（`api/main.py:398-417`）；`/health` 仍 `status=ok`（`api/main.py:236-263`） |
| **本地复核** | `AUTH_ENABLED` 未启用时 `authenticate(None)` → `development-system` + `admin`（已复现） |
| **影响** | 漏配密钥的试点服务允许匿名读改任务、完成 handoff、代审批；重启丢失任务与供应商证据；健康检查不报警 |
| **修复** | 引入 `APP_ENV`；`internal`/`production` 启动必须：PostgreSQL、耐久对象存储、`AUTH_ENABLED=true`、签名密钥、显式 provider；仅 `local` 允许 no-auth；拆分 `/livez`/`/readyz` |
| **验证** | 生产 profile 缺任一项启动失败；未认证请求 401；kill/restart 后任务与 raw object 仍在 |

#### C2 — D4 安全/溯源断言被硬编码为“通过”

| 字段 | 内容 |
|---|---|
| **来源** | AI Engineer（API/Security 交叉关注评测可信度） |
| **证据** | `services/evaluation_agent_eval.py:1304-1317`（及 deterministic 路径约 631-636）将 `trajectory.unsupported_parameter_value_count` 与 `security.untrusted_instruction_mutations` 设为字面量 `0`；而用例 builder 要求这些为 0 才过 hard assert（`examples/build_agent_eval_v1.py` 相关断言） |
| **本地复核** | 源码中存在 `trajectory={"unsupported_parameter_value_count": 0}` 与 `security={"untrusted_instruction_mutations": 0}` |
| **影响** | 24/24、24×3 对 **workflow/状态/工具序** 仍有意义，但 **不能** 作为“参数溯源安全”或“抗指令注入未改写槽位”的证据；被操纵的模型改城市/约束仍可能显示安全断言全绿 |
| **修复** | 从槽位 lineage / 用户轮次 / 安全默认 / task-tool delta **实测**计数；无法测量时返回 `not_evaluable` 并 fail-closed 或单独门禁 |
| **验证** | mutation test：合法 schema 但未 grounding 的 destination 必须让安全/溯源门禁失败 |

#### C3 — 无已证明的生产部署、渐进发布、回滚与灾备路径

| 字段 | 内容 |
|---|---|
| **来源** | SRE（Backend 部分重叠） |
| **证据** | 工作区 **无 `.git`、无 `.github`/CI**、无 Dockerfile/应用部署清单；`compose.yaml` 仅开发 Postgres；`README` 推荐 `uvicorn --reload`；迁移 downgrade 可 drop 表（`migrations/versions/0001_task_persistence.py`） |
| **影响** | 无法 canary/回滚；卷或库丢失无 RPO/RTO；“本机 342 测过”不等于持续交付门禁 |
| **修复** | 不可变镜像、探针/资源/PDB、expand-contract 迁移、托管库 PITR、对象存储恢复、回滚演练 runbook |
| **验证** | 坏版本 canary 回滚不丢任务；声明 RTO 内恢复库与对象证据 |

> 注：C3 对“纯本地 demo”可降为 High；对任何网络可达试点/生产仍按 Critical 处理。

---

### High

#### H1 — LLM `429`/`5xx` 被标为不可重试，且失败后强制 structured（与 taxonomy/HANDOFF Step2 矛盾）

| 字段 | 内容 |
|---|---|
| **来源** | Workflow, AI Engineer |
| **证据** | `agent/openai_adapter.py:315-324`：任意 HTTP status → `retryable=False`；API 默认单次 LLM 尝试（`demo.py`）；taxonomy 要求耗尽后 `CLARIFY`（`error_recovery.py:154`）；编排器恒转 `NEEDS_STRUCTURED_INPUT` 且无澄清问题（`orchestrator.py:410-437`）；单测固化 structured（`test_intent_scenarios.py:524`） |
| **本地复核** | `_classified_openai_error`：429/503/408 → `retryable=False` |
| **影响** | 短暂限流直接把对话打成表单；评测与生产的 SDK 重试行为也不一致 |
| **修复** | 408/429/部分 5xx 可重试 + `Retry-After`；禁用隐藏 SDK 重试；耗尽后优先澄清；structured 仅永久失败/多次澄清失败 |
| **验证** | 注入 429→成功：两次链接 LLM 记录且不进 structured |

#### H2 — `OUT_OF_SCOPE` 由模型一锤定音且不可逆，修订前清空行程产物

| 字段 | 内容 |
|---|---|
| **来源** | Workflow, AI Engineer |
| **证据** | `param_extraction_loop.py` 接受 OOS；`orchestrator.py:270-278` 在后搜索修订中先清 options/selection/booking/request；`orchestrator.py:541-550` 立即 `OUT_OF_SCOPE`；`submit_message` 不允许 OOS；状态机 `OUT_OF_SCOPE: frozenset()`（`state_machine.py:65`） |
| **影响** | “改早一点”被误标 OOS → 当前任务永久作废，搜索结果丢失 |
| **修复** | copy-on-write 修订；有 grounded 槽位时拒绝纯 OOS；允许显式“新意图”`OUT_OF_SCOPE → DRAFT` |
| **验证** | `WAITING_FOR_USER` 返回 OOS 后旧 options 仍可恢复；合法新行程可重开 |

#### H3 — 跨进程/跨 worker 报价缓存仅内存，重验必然 `UNAVAILABLE`

| 字段 | 内容 |
|---|---|
| **来源** | Backend（SRE 多 worker 问题相关） |
| **证据** | Duffel/LiteAPI `_offer_cache` 进程内字典；引用不在缓存则拒绝重验（`duffel.py`/`liteapi.py` 搜索后与 revalidate 路径） |
| **影响** | Worker A 搜索、Worker B 选择/审批，或等待审批期间重启 → 无法 handoff |
| **修复** | 持久化加密的 revalidation context（按 task/snapshot/reference）；禁止依赖粘性路由 |
| **验证** | 两实例共享 Postgres：实例 1 搜索，实例 2 选择/审批应触达供应商重验 |

#### H4 — 崩溃恢复只看 `STARTED` 工具记录，遗漏大量瞬时态

| 字段 | 内容 |
|---|---|
| **来源** | Backend, SRE |
| **证据** | 进入 `SEARCHING`/`REVALIDATING` 可在工具 `STARTED` 前落盘；恢复仅扫描含 `STARTED` 的瞬时任务（`orchestrator.py:1465-1480`） |
| **影响** | 卡在 `SEARCHING`/`REVALIDATING` 且无 STARTED → 重启后永久悬挂 |
| **修复** | 操作 lease/checkpoint；恢复所有超时瞬时态；结果/outbox 尽量原子 |
| **验证** | 在各持久化边界 kill 后重启，任务必达可操作终态或可恢复态 |

#### H5 — 同步 Provider 重试可长时间占满 API worker（约 3×65s）

| 字段 | 内容 |
|---|---|
| **来源** | Workflow, Backend, API Tester, SRE |
| **证据** | Duffel timeout 65s；编排器最多 3 次即时尝试 + sleep；FastAPI 同步调用 workflow |
| **影响** | 上游黑洞时请求线程被占满；客户端先超时，看不到 `WAITING_FOR_PROVIDER` |
| **修复** | 总同步 SLA、细粒度超时、bulkhead；长恢复走持久化后台并 `202` |
| **验证** | 并发黑洞：p99/线程数有界，liveness 仍响应 |

#### H6 — 延迟重试队列无 per-task 隔离 + 全表扫描；`/health` 非就绪探针

| 字段 | 内容 |
|---|---|
| **来源** | Workflow, Backend, SRE |
| **证据** | `process_due_provider_retries` 循环无 per-task try/except（`orchestrator.py:1203-1235`）；`list_tasks()` 加载全库；历史 policy 缺失会永久 raise；`/health` 恒 ok |
| **影响** | 一条毒任务可挡住后续 due 任务；健康检查仍绿 |
| **修复** | per-task 捕获并标记 `PROVIDER_FAILED`/`RECOVERY_FAILED`；due 索引 + `SKIP LOCKED`；`/readyz` 查 DB + scheduler 心跳 |
| **验证** | 毒任务 + 健康任务同 poll：前者失败、后者恢复；DB 断连 ready 失败 |

#### H7 — 多腿 partial recon 仅诊断：恢复重跑成功腿并吞预算

| 字段 | 内容 |
|---|---|
| **来源** | Workflow |
| **证据** | taxonomy 要求只重试失败腿；实现清 options 后整段 `_search_and_plan`；snapshot 仅全成功后挂载；单测只验 taxonomy 文案 |
| **影响** | 酒店失败导致航班重搜，12 次工具预算提前耗尽，延迟重试跑不满 |
| **修复** | 每腿成功即落证据；resume plan 只补缺失/过期腿 |
| **验证** | 酒店失败注入：仅酒店再调、航班 snapshot 保留、预算内完成 |

#### H8 — `provided_fields` 由模型自证，非文本 grounding

| 字段 | 内容 |
|---|---|
| **来源** | AI Engineer, Security（补全） |
| **证据** | schema 允许模型填写 `provided_fields`；merge 对列出的字段大体直接接受；L2 验形状/业务而非原文支撑 |
| **影响** | 注入或幻觉可产生“合法但未在用户话中出现”的城市/日期并触发搜索 |
| **修复** | 槽位级 evidence（turn/span/derivation）；无法对齐则澄清 |
| **验证** | 未 grounding 城市 → `NEEDS_CLARIFICATION`、0 次 provider、unsupported 指标 >0 |

#### H9 — 生产无模型/请求可观测；现有 trace 含自由文本风险

| 字段 | 内容 |
|---|---|
| **来源** | AI Engineer, SRE |
| **证据** | API 构建 workflow 无 `trace_observer`；observability 服务为离线 backfill；redaction 未覆盖任意 message 字段 |
| **影响** | 漂移/重试风暴/成本不可见；直接挂现有 recorder 可能违规存用户原文 |
| **修复** | 生产 observer：指标/哈希/token/成本，非常规落原文；定义 SLO 与告警 |
| **验证** | 集成测试事件齐全，canary 密钥与用户句不出现在遥测 |

#### H10 — API 测试可在导入时绑定真实 Provider

| 字段 | 内容 |
|---|---|
| **来源** | API Tester |
| **证据** | `api/main.py` 模块级 `travel_provider_from_environment`；`test_api.py` 直接 import app 建 trip，未强制 Mock |
| **影响** | 带 `TRAVEL_PROVIDER=duffel*` 的 shell 跑 pytest 可能打沙箱/只读额度 |
| **修复** | app factory + 注入；fixture 隔离 mock；CI 禁网 |
| **验证** | 外部样貌 env + 禁网哨兵下 external request count=0 |

#### H11 — 非 JSON 的 429/5xx 先被解析为终端 `ProviderError`

| 字段 | 内容 |
|---|---|
| **来源** | API Tester |
| **证据** | Duffel/LiteAPI `_successful_payload` 先 `_json_payload`，无效 JSON 抛普通 `ProviderError`，不进 `RetryableProviderError` 分支（`duffel.py:568-600`，`liteapi.py:653-686`） |
| **影响** | HTML/空 body 限流不触发熔断与延迟恢复 |
| **修复** | 先按 status 分类，再 best-effort 解析；归档有界原始字节 |
| **验证** | 空/HTML 429、503 保持可重试并进入调度 |

#### H12 — 任务创建无传输层幂等；创建与首审计非原子

| 字段 | 内容 |
|---|---|
| **来源** | Backend, API Tester |
| **证据** | POST 每次新 UUID；task insert 与首 audit 分事务 |
| **影响** | 客户端重试双任务双搜索；崩溃可留下无审计 DRAFT |
| **修复** | `Idempotency-Key` 唯一约束；原子 `TASK_CREATED` |
| **验证** | 同 key 并发 → 单任务单 provider 序列 |

#### H13 — 失败的计费模型响应不入成本账本；无独立 LLM 预算

| 字段 | 内容 |
|---|---|
| **来源** | AI Engineer |
| **证据** | 仅成功 metadata 入 ledger；失败路径无 token；与 provider 共享 12 次 tool 预算 |
| **影响** | 成本低估；repair 可先耗尽预算 |
| **修复** | 失败也记账；`complete/lower-bound` 状态；独立 LLM cap |
| **验证** | 无效 JSON 但含 usage → 失败 ledger 行仍计成本 |

---

### Medium

| ID | 标题 | 来源 | 摘要 |
|---|---|---|---|
| M1 | 共享熔断器覆盖 Duffel+LiteAPI；跨进程不共享 | Workflow, API, SRE | LiteAPI 故障可压制 Duffel；多 worker 各自打上游 |
| M2 | 真实 full-chain runner 无法验收延迟恢复 | Workflow, API | 无 scheduler、无 wait due、关客户端后看终态；HANDOFF P0 未闭环 |
| M3 | 失败 HTTP 响应未归档；LiteAPI 204 合成 `{"data":[]}` | Backend, API | 审计/回放缺失败证据；204 hash 非原文 |
| M4 | OpenAPI 为 `dict`/无错误契约；创建 200 而非 201/202 | Backend, API | 客户端与状态语义不稳定 |
| M5 | 登录无速率限制（scrypt） | API, Security | CPU DoS / 暴力尝试 |
| M6 | 列表未分页 + 全量反序列化 | Backend, SRE | 规模上升后延迟与内存线性恶化 |
| M7 | 对象存储 retention 仅元数据；无生命周期/容量 | Backend, SRE | 磁盘膨胀；非 Object Lock |
| M8 | 多轮替换语义靠窄正则；provenance 每轮重建 | AI | “Actually make it A to B” 不触发清槽 |
| M9 | Chat/Responses 模式靠模型名子串推断 | AI | 中性 alias 可能选错协议 |
| M10 | 文档/路线图/状态图与代码漂移 | Workflow | `architecture` 漏边；requirements/roadmap 陈旧 |
| M11 | 优雅停机不排空、不 close provider/DB | Backend, SRE | SIGTERM 可砍 65s 调用 |
| M12 | Test Order 写路径无 mutation ledger | API | 超时后可能残留未取消 sandbox order |
| M13 | 无 fairness/bias 评测切片 | AI | 跨语言/职级差异不可见 |
| M14 | 请求字段边界不完整（OptionSelection 等无 `extra=forbid`） | API | 超长/未知字段 silently accept |

---

### Low

| ID | 标题 | 来源 |
|---|---|---|
| L1 | Compose Postgres 浮动 tag、无资源/日志策略 | SRE |
| L2 | HANDOFF 仍写“无 --case-id”，CLI 已支持 | Workflow |
| L3 | 无偏见外的更多文档对账检查清单 | Workflow |

---

## 5. Security Architect 补全结论

Codex 子代理 `security_audit` 因 usage limit 未完成 FINAL_ANSWER。以下为本地只读补全（与 C1/C2/H8/H10/M5 对齐）：

### 安全优势

- 启用 auth 时：scrypt、HMAC 短时会话、资源可见性、枚举安全 404。
- 供应商：test token / live 标志硬拦；写操作不进自动重试。
- Raw response：不可变引用 + hash + 访问策略（在配置目录时）。

### 安全优先修复顺序

1. **C1 fail-open 启动策略**（必须先于任何网络暴露）  
2. **C2 评测安全断言实测化**（否则“安全门禁通过”不可信）  
3. **H8 grounding / provenance**（降低注入与幻觉写槽）  
4. **H10 测试环境禁真实 Provider**  
5. **M5 登录限流 + M14 输入边界**

### 明确不在本轮声称

- 未做完整 STRIDE/渗透  
- 未做密钥轮换、密钥托管、依赖 CVE 扫描  
- 未验证生产 TLS/网关/WAF  

---

## 6. 验证证据（本轮本地）

### 6.1 自动化测试

```bash
# 注意：若 shell 中已 source .env 且 DATABASE_URL 指向不可用 Postgres，
# 应用导入会在收集阶段失败。审计复现使用：
env -u DATABASE_URL .venv/bin/python -m pytest -q
```

**结果：** 全量通过（342 项量级；仅 Starlette/httpx TestClient 弃用警告）。

定向子集（API / auth / retry / persistence / error_recovery / intent / object_storage）在无 `DATABASE_URL` 时亦通过。

### 6.2 针对性探针

| 探针 | 结果 |
|---|---|
| Auth fail-open → admin | **确认** |
| LLM HTTP 429/503/408 `retryable` | **全部 False** |
| Eval hard-zero unsupported/untrusted | **确认** |
| 无 `.git` / 无 `.github` | **确认** |
| `ruff`（抽检 auth/api/adapter/eval） | 通过 |
| 非 JSON 429 分类顺序 | 代码确认：先 JSON 解析 |

### 6.3 未做（有意）

- 真实 Duffel/LiteAPI 故障注入与延迟恢复验收（需授权 + 新报告目录）  
- 多进程共享 Postgres 的跨 worker 重验实测  
- 计费真实模型回归  

### 6.4 对“测试全绿”的正确解读

| 含义 | 是否成立 |
|---|---|
| 确定性单测与当前 mock 契约一致 | 是 |
| 默认配置可安全暴露到网络 | **否**（C1） |
| 评测安全断言已真实测量 | **否**（C2） |
| 延迟恢复已在真实上游验收 | **否**（M2 / HANDOFF） |
| 具备 CI/CD 与回滚证据 | **否**（C3） |

---

## 7. 与 HANDOFF 三项下一步的对照

| HANDOFF 项 | 审计结论 | 关联 finding |
|---|---|---|
| 真实 Provider 延迟恢复验收 | **仍未验收**；现有 runner 结构上无法跑完整延迟链路 | M2, H5, H6 |
| §13 Step2：LLM 失败优先澄清 | **未实现**；taxonomy 写 CLARIFY，运行时 structured；429 不可重试 | H1 |
| OOS 收紧 / 重开 | **未实现**且具破坏性（先清空再 OOS） | H2 |

---

## 8. 建议修复优先级（执行序）

### P0 — 任何网络可达部署前

1. **C1** 生产 profile fail-closed（auth + 持久化 + ready 探针）  
2. **C2** 去掉评测安全/溯源假阳性  
3. **H10** 测试与 app factory 隔离真实 Provider  
4. **H3** 持久化 revalidation context（否则多实例/重启必挂）  

### P1 — 试点可用性与 HANDOFF 主线

5. **H1** LLM 瞬时错误分类 + 澄清优先  
6. **H2** OOS 非破坏与可重开  
7. **H5/H6** 同步 SLA、per-task 调度隔离、readyz  
8. **H11** Provider 先 status 后 parse + 失败归档  
9. **M2** 专用延迟恢复验收 harness（明确授权）  

### P2 — 生产化

10. **C3** Git/CI/镜像/迁移/回滚/PITR  
11. **H4/H7/H9/H12/H13** 崩溃恢复、partial resume、观测、幂等、成本完整性  
12. **M\*** 契约、限流、分页、Object Lock、熔断分 upstream  

---

## 9. 角色级摘要（未展开细节见 `_audit-wip`）

| 角色 | 总评摘要 |
|---|---|
| Workflow Architect | Review required；无 Critical，但 H1–H4 + 延迟验收缺口关键 |
| Backend Architect | ~48/100 网络试点阻塞；fail-open 与报价缓存为首要 |
| AI Engineer | 确定性边界强；安全门禁假阳性与 grounding 阻塞生产可信 |
| API Tester | 供应商硬边界好；鉴权默认与错误分类/测试隔离是主风险 |
| SRE | 内部监督试点约 61；公网约 32；无部署/SLO/多 worker 安全 |
| Security Architect（补全） | 与 C1/C2/H8 对齐；启用 auth 后设计合理，默认配置不可外放 |

---

## 10. 附录

### 10.1 产出路径

| 文件 | 说明 |
|---|---|
| `reports/agency-agents-role-audit-2026-08-10.md` | **本合并报告** |
| `reports/_audit-wip/workflow.md` | Workflow 原始产出 |
| `reports/_audit-wip/backend.md` | Backend 原始产出 |
| `reports/_audit-wip/ai.md` | AI Engineer 原始产出 |
| `reports/_audit-wip/api.md` | API Tester 原始产出 |
| `reports/_audit-wip/sre.md` | SRE 原始产出 |

### 10.2 Codex 会话

- Session：`019febaf-c9ab-7170-8732-dda0417c58d9`  
- 中断点：Security Architect 深度扫描过程中 usage limit；最终 Markdown 当时未落盘（`reports/agency-agents-role-audit-2026-08-10.md` 不存在）  
- 本报告：续作完成 API/SRE 已有结论的合并、Security 补全、本地验证与落盘  

### 10.3 建议的下一动作（实现向，非本报告范围）

若进入修复阶段，建议按 **P0 四条** 开独立变更，每条带启动/鉴权/评测 mutation 单测；真实延迟恢复单独授权执行，新建 `reports/evaluation-runs/...` 目录，勿覆盖历史证据。

---

*审计完成时间：2026-08-10。本报告为只读审计结论，不包含代码修复。*
