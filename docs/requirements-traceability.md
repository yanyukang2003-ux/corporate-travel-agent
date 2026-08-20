# 需求追踪

本表把两份输入文档中的关键要求映射到当前框架，避免项目退化成普通问答 Demo。

| 基线要求 | 当前实现 | 位置 | 状态 |
|---|---|---|---|
| 单 Agent 编排器 | 显式工作流控制器 | `agent/orchestrator.py` | 已实现 |
| LLM 不判断合规 | LLM Protocol 与政策引擎隔离 | `agent/ports.py`, `policy/engine.py` | 已实现 |
| 完整行程组合 | 去程、返程、酒店笛卡尔组合后硬过滤 | `planning/planner.py` | 已实现 |
| 可行性硬校验 | 路线、时间窗、缓冲、酒店覆盖、可用性 | `planning/feasibility.py` | 已实现 |
| 版本化政策和证据 | PolicySnapshot 与 RuleEvidence | `domain/models.py` | 已实现 |
| 可替换 Provider | Protocol + Mock + Replay | `providers/` | 已实现 |
| 供应商失败安全降级 | 3 次即时尝试、60 秒熔断、`WAITING_FOR_PROVIDER` 与最多 3 次持久化延迟重试；耗尽进入 `PROVIDER_FAILED` | `agent/orchestrator.py`, `services/provider_resilience.py` | 已实现 |
| 分布式延迟重试所有权 | PostgreSQL 原子 claim、lease expiry 接管、fencing token 写回与专用 worker 角色 | `services/sqlalchemy_repository.py`, `agent/orchestrator.py`, `api/main.py` | 已实现；PostgreSQL 并发压测待生产环境留证 |
| 例外审批暂停/恢复 | 绑定主题哈希和指定审批人 | `agent/orchestrator.py` | 已实现 |
| 价格/库存重验 | 选择或批准后强制 revalidate | `agent/orchestrator.py` | 已实现 |
| 跨进程重验上下文 | 无凭证上下文在搜索返回前写入内存/SQL store；缺失或过期 fail closed，重验仍实时请求 Provider | `services/provider_quote_context.py`, `services/sqlalchemy_repository.py`, `providers/duffel.py`, `providers/liteapi.py` | 已实现 |
| 幂等 Booking Intent | 由任务/请求/方案版本生成键 | `agent/orchestrator.py` | 已实现 |
| 审计轨迹 | 状态、快照、规则、审批和重验事件 | `services/audit.py` | 已实现（内存/PostgreSQL） |
| 自然语言意图提取 | Responses API + Pydantic Structured Outputs | `agent/openai_adapter.py`, `agent/schemas.py` | 已实现，待真实凭证冒烟 |
| 最多三轮澄清 | 跨轮字段合并、确定性问题、结构化降级 | `agent/orchestrator.py` | 已实现 |
| 每任务最多 12 次工具调用 | 统一预留、成功/失败计数、审计与终止态 | `agent/orchestrator.py`, `domain/models.py` | 已实现 |
| Prompt Injection 边界 | 严格 schema、字段白名单、操纵标记 | `agent/schemas.py`, `tests/test_intent_scenarios.py` | 已实现 |
| 城市名称一致性 | 外部城市代码目录将中英文别名映射到 Provider 规范名称 | `services/policy_config.py`, `services/locations.py` | 已实现 |
| 企业政策与审批配置 | 严格 JSON schema、跨引用校验、当前/历史快照、内容哈希固定 | `services/policy_config.py`, `config/travel-policy.json` | 已实现 |
| 无可行方案可解释 | 区分去程、返程、酒店无库存与硬过滤 | `agent/orchestrator.py` | 已实现 |
| 库存与交接有效期 | 共享时钟、快照/链接过期拦截与审计 | `providers/mock.py`, `agent/orchestrator.py` | 已实现 |
| 20+ 真实快照 | 脱敏数据集、WORM 导出、清单哈希、去重统计和查询契约已具备；Mock 样例 3 个 | `services/replay_dataset.py`, `services/redaction.py`, `docs/replay-datasets.md` | 工具已实现，真实来源 0/20 |
| 60+ 场景评估 | 540 条派生案例：60 条工作流案例、480 条完整测试切分意图案例；按旧版/扩展 cohort、来源和场景报告分类、字段、澄清、越界、交通偏好、不支持约束拒绝、提前调用与幻觉指标；另有火车、多职级、禁止/证据不足、审批、跨时区和多币种边界回归 | `data/evaluation/derived-v2`, `services/evaluation_dataset.py`, `tests/test_evaluation_dataset.py`, `tests/test_enterprise_edge_cases.py` | 完整离线测试切分与确定性执行已完成，真实模型质量评估待完成 |
| 认证和资源级授权 | scrypt 登录、短期签名会话、员工本人/直属审批人/管理员范围，审批身份由令牌派生 | `services/auth.py`, `api/main.py` | 内部试用已实现，正式环境待接 OIDC/SSO |
| PostgreSQL 任务持久化 | JSONB 聚合、事务审计、乐观锁、迁移和重启恢复 | `services/sqlalchemy_repository.py`, `migrations/` | 已实现 |
| Provider 原始响应对象存储 | 内存/本地 WORM 后端、不可变引用、保留期、受限读取与公开字段脱敏 | `services/object_storage.py`, `providers/mock.py` | 内部试用已实现，正式环境待接 S3 Object Lock |
| Browser-assisted 查询 | 已固定接口与安全边界 | — | V1.5 |
| 正式 TMC API | 不假设已有权限 | — | 获权后实现 |
