# 新旧意图链路隔离改造汇总

## 目标

保留原有自然语言链路作为回滚能力，同时新增一条完整对话语义链路。两条链路在入口、
任务身份、编排方法和模型端口上彼此独立，不通过运行时 mode 切换，也不互相回退。

## 入口矩阵

| 用途 | 创建入口 | 后续消息入口 | 固定任务标记 |
|---|---|---|---|
| 结构化请求 | `POST /trip-tasks` | 不接受自然语言续接 | `structured` |
| 原有自然语言链路 | `POST /legacy/trip-tasks` | `POST /legacy/trip-tasks/{task_id}/messages` | `legacy` |
| 新完整语义链路 | `POST /semantic/trip-tasks` | `POST /semantic/trip-tasks/{task_id}/messages` | `semantic` |

任务创建后，`intent_entrypoint` 不随消息改变。向错误入口提交后续消息会失败；系统不会
猜测调用者本来想走哪条链路。

## 调用链

旧链路：

```text
legacy route
  -> create_task_from_message / submit_message
  -> LanguageModelPort.extract_trip_intent
  -> 字段合并、证据校验、校准、旧澄清逻辑
  -> TripRequestVersion
```

新链路：

```text
semantic route
  -> create_task_from_semantic_message / submit_semantic_message
  -> SemanticLanguageModelPort.interpret_trip_intent
  -> ConversationLedger
  -> IntentDecision
  -> compile_search_command
  -> TripRequestVersion
```

两条链路仅在形成并验证 `TripRequestVersion` 之后，共享库存搜索、政策、审批、持久化和
交接能力。新链路不会调用旧字段抽取，也不会在语义解释失败时降级为旧解析器。

## 主要代码改动

- `domain/enums.py`：增加 `IntentEntrypoint`，定义 `structured`、`legacy`、`semantic`。
- `agent/ports.py`：新增只包含完整语义解释能力的 `SemanticLanguageModelPort`；旧
  `LanguageModelPort` 继续承载旧字段抽取能力。
- `agent/openai_adapter.py`：拆分 `OpenAIResponsesLanguageModel` 与
  `OpenAISemanticIntentLanguageModel`。两个适配器不公开对方的意图方法。
- `agent/semantic_intent.py`：新语义入口只读取完整对话账本并验证模型证据；删除旧抽取
  回退和 mode 分支。
- `agent/orchestrator.py`：增加显式的新旧创建与续接方法，任务固定记录入口，并拒绝跨入口
  调用。
- `api/main.py`：增加 `/legacy` 与 `/semantic` 路由族；通用 `/trip-tasks` 只保留结构化
  创建。
- `frontend/src/api/client.ts`：为新旧路由提供不同客户端方法。
- `frontend/src/App.tsx`：新建自然语言任务默认走语义入口；历史 `legacy` 任务继续走旧续接
  入口。
- `tests/test_semantic_intent.py`、`tests/test_api.py`：覆盖模型接口隔离、入口固定、跨入口拒绝
  和 OpenAPI 路由。
- `docs/architecture.md`、`docs/adr/0002-single-owner-semantic-intent.md`、`README.md`：记录
  新旧链路边界、使用方式与旧链路删除门槛。

## 保留与删除策略

- 本次不删除旧解析器、校准器、旧澄清逻辑或旧 API。
- 不再使用 `INTENT_ARCHITECTURE_MODE` 在同一个入口中切换实现。
- 旧任务缺少 `intent_entrypoint` 时按 `legacy` 兼容，避免历史持久化任务失效。
- 只有 ADR-0002 中的语义评测、错误搜索、回归与观察期门槛全部满足后，才考虑单独删除
  旧链路。

## 验收范围

验收包含：语义/API 边界测试、完整 Python 测试、Python 静态检查、前端生产构建以及
Git 空白错误检查。不会在本地验收中触发真实预订或计费模型调用。


---

**2026-09-01 收尾（ADR-0003）。** 这份文档记录的迁移已经走完并且超越了它的目标：语义入口和它
要替换的旧入口**都已删除**。产品入口是工具循环（`/agentic/trip-tasks`）；冻结评测集经它执行
（D16），删除门槛的证据见 `docs/adr/0003-single-product-entrypoint.md`。文中的路径和 runner
名字保留为历史记录，不再对应仓库里的代码。
