# PostgreSQL 运维手册（Phase A–D）

本项目任务持久化以 **聚合 JSONB + 查询投影列** 为主，配置与 Outbox 为可选扩展。

## 1. 本地启动

```bash
cd corporate-travel-agent
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e '.[dev,persistence]'

docker compose up -d postgres
export DATABASE_URL='postgresql+psycopg://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent'

# 生产/预发禁止 DATABASE_AUTO_CREATE；一律用 Alembic
alembic upgrade head

export DATABASE_AUTO_CREATE=false
uvicorn corporate_travel_agent.api.main:app --reload
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

期望：

- `persistence` 为 `postgresql`
- `persistence_details.alembic_version` 有值（经 Alembic 迁移后）
- `outbox.unpublished_count` 为数字

重启恢复冒烟：

```bash
.venv/bin/python examples/verify_postgres.py
```

## 2. 环境变量矩阵

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 空 → 内存仓储 | 例：`postgresql+psycopg://user:pass@host:5432/db` |
| `DATABASE_AUTO_CREATE` | `false` | 仅临时开发；`ENVIRONMENT=production/staging` 时禁止 |
| `ENVIRONMENT` | `development` | `production/prod/staging` 会强制持久仓储、认证、签名密钥和原文存储，缺一项即拒绝启动 |
| `DATABASE_POOL_SIZE` | `5` | 连接池大小（非 SQLite） |
| `DATABASE_MAX_OVERFLOW` | `10` | 池溢出 |
| `DATABASE_POOL_TIMEOUT` | `30` | 取连接超时秒 |
| `DATABASE_POOL_RECYCLE` | `1800` | 连接回收秒 |
| `DATABASE_POOL_PRE_PING` | `true` | 断线探测 |
| `POLICY_CONFIG_BACKEND` | `file` | `file` 或 `postgres` |
| `POLICY_CONFIG_FILE` | 内置 demo | 文件后端路径 |
| `POLICY_CONFIG_BOOTSTRAP_FILE` | 空 | PG 无 active 配置时一次性导入 |
| `RAW_RESPONSE_STORE_DIR` | 空 → 内存 | Provider 原文 WORM 目录 |
| `OPENAI_TIMEOUT_SECONDS` | `60` | 单次模型 HTTP 调用上限；SDK 内部重试关闭，由编排器统一重试 |
| `MAX_CONCURRENT_LLM_CALLS` | `8` | 单进程模型调用舱壁容量 |
| `MAX_CONCURRENT_PROVIDER_CALLS` | `16` | 单进程供应商调用舱壁容量 |
| `TOOL_ACQUIRE_TIMEOUT_SECONDS` | `5` | 等待舱壁槽位的最长秒数 |
| `INTERRUPTED_TASK_STALE_SECONDS` | `30` | 仅恢复超过该时长的中间态任务，避免启动中的其他 worker 被误判为崩溃 |
| `FLIGHT_STATUS_SOURCE` | `none` | 航班动态源：`none` 不查；`memory` 进程内表（接推送 / 演示） |
| `TRIP_WATCH_POLL_SECONDS` | `60` | watch worker 轮询间隔 |
| `TRIP_WATCH_LEASE_SECONDS` | `300` | 领取一趟差旅的租约；保存即释放 |
| `TRIP_WATCH_LOOKAHEAD_HOURS` | `48` | 起飞前多少小时开始盯 |
| `TRIP_CHANGE_MIN_CONNECTION_MINUTES` | `60` | 前一段落地到下一段起飞至少留多久才算接得上 |
| `FLIGHT_DELAY_NOTICE_MINUTES` | `15` | 延误多少分钟以内不打扰旅行者 |
| `FLIGHT_STATUS_WEBHOOK_SECRET` | 空 → 不收推送 | `POST /flight-status/webhook` 的 HMAC-SHA256 密钥 |
| `TRIP_WATCH_WORKER_ID` | 随机 | 多 worker 时区分租约持有者 |

## 3. 迁移纪律

```bash
# 开发 / CI / 生产统一
alembic upgrade head
alembic current
```

| 修订 | 内容 |
|---|---|
| `0001_task_persistence` | 任务/审计/库存快照 |
| `0002_raw_response_objects` | 原文对象引用列 |
| `0003_task_query_projections` | 员工/经理/重试等投影列与索引 |
| `0004_config_and_outbox` | 政策配置、员工/政策快照行、Outbox |

**禁止**：在生产对空库 `create_all` 作为唯一 schema 来源。  
**测试**：SQLite 可用 `create_schema()`；真 PG 应用 Alembic。

## 3.1 载荷版本与升级

每条 JSON 载荷带 `schema_version`（当前 3）。**模型改了形状必须同时在
`services/serialization.py` 加一级升级函数**，读取时旧载荷逐级升到当前版本；写入永远是当前版本。
版本 1 → 2 是 2026-08-29 方案从 `outbound/inbound/hotel` 改成 `legs/stays`。

- 读取：升级后仍不合模型的行抛 `PayloadIncompatible`；`GET /trip-tasks/{id}` 给 500 并说明
  该跑哪个工具，`GET /trip-tasks?summary=false` 跳过该行并记日志（摘要列表走投影列，不受影响）。
- 批量改写：`examples/upgrade_task_payloads.py`（默认干跑，`--apply` 真改），四张载荷表都过一遍，
  `revision` 列不动，任务表同时更新 `payload_schema_version`。**先 `pg_dump`。**
- 版本比当前代码新（回滚代码之后）一律拒绝读：`UnsupportedPayloadVersion`。

| 迁移 | 内容 |
|---|---|
| `0010_trips` … `0011_provider_circuit_state` | 差旅聚合、共享熔断 |
| `0012_trip_watch_schedule` | 差旅观察排程与租约 |
| `0013_task_trip_projection` | `trip_tasks.trip_id` 投影 + 回填，差旅按任务反查走索引 |

## 3.2 一笔事务的边界

| 动作 | 同一笔里的东西 |
|---|---|
| 建规划 / 改期任务 | 任务行 + 差旅行（新建或乐观锁更新） |
| 任何状态变化 | 任务行 + 审计行 + 发件箱行 |
| 下单确认 | 状态迁移 + 确认记录 + 确认审计 + 发件箱通知 + 差旅观察对象（`record_with_trip`，迁移审计作为 `preceding` 同笔） |
| 航班动态观察 / 取消差旅 | 差旅行 + 被观察任务的审计 + 发件箱 |

两张表共用一个引擎才能一笔；任务在 SQL、差旅在内存（单测常见）时退回两笔。

## 4. 备份与恢复

### 逻辑备份

```bash
# 需要本机 psql/pg_dump 客户端
pg_dump "$DATABASE_URL_WITHOUT_DRIVER" -Fc -f travel_agent_$(date +%Y%m%d).dump
```

`DATABASE_URL` 若为 SQLAlchemy 形式，请换成 `postgresql://user:pass@host:5432/db` 给 `pg_dump`。

### 恢复

```bash
pg_restore -d postgresql://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent \
  --clean --if-exists travel_agent_YYYYMMDD.dump
alembic current   # 确认版本表完好
```

### Compose 卷

开发数据在 Docker 命名卷 `travel-agent-postgres`：

- `docker compose stop postgres`：停服务，**保留**数据  
- `docker compose down -v`：**删除**卷，数据不可恢复  

### RPO / RTO 建议（内部试用）

| 场景 | RPO | RTO |
|---|---|---|
| 开发本机 | 可接受丢失 | 重建 compose + migrate |
| 内部试用 | 日备 ≤ 24h | 恢复 dump + 重启 API ≤ 30min |
| 生产目标 | 连续 WAL / PITR | 按基础设施 SLA |

原文对象不在 PG 内：备份 `RAW_RESPONSE_STORE_DIR` 或未来 S3 桶。

`provider_quote_contexts` 保存 Duffel/LiteAPI 重验所需的最小上下文，主键为
`(provider, snapshot_id, ref_id)`；`(provider, ref_id, expires_at)` 支持跨 worker 查找，
`expires_at` 索引用于 TTL 清理。该表不保存 API key、Authorization header 或其他凭证，且
记录到期后不能绕过 Provider 实时重验继续使用。

## 5. 查询与调度（Phase B）

投影列写在 `trip_tasks`：`employee_id`、`manager_id`、`next_retry_at` 等。  
写路径 `add` / `record` 同步投影；聚合 JSONB 仍是事实源。

| API | 行为 |
|---|---|
| `GET /trip-tasks?summary=true` | 默认摘要列表（不含 options/tool traces） |
| `GET /trip-tasks?summary=false` | 完整公开任务文档 |
| `GET /approvals/inbox` | 审批人待办（`WAITING_FOR_APPROVAL`） |
| 后台 `process_due_provider_retries` | 使用 `list_due_provider_retries` + 索引 |

生产延迟重试使用 `claim_due_provider_retries`：同一事务中以 `FOR UPDATE SKIP LOCKED` 选择
到期或 lease 已过期的任务，并写入 owner、lease 和 fencing token。API 进程使用
`PROCESS_ROLE=api`；独立消费者使用 `PROCESS_ROLE=worker`。默认 lease 为 900 秒，旧 token
的写回不会覆盖接管 worker 的结果。健康检查公开 claimed、lease conflict、reclaimed、
completed、exhausted 和 oldest due lag 指标。

## 5.1 默认币种（USD）

企业政策与领域模型默认币种为 **USD**（对齐 Duffel Test 报价）。  
修改 `config/travel-policy.json` 后若使用 Postgres 配置后端，需重新导入：

```bash
.venv/bin/python examples/import_policy_config.py --file config/travel-policy.json
```

评测 fixture（agent-eval worlds）可仍为 CNY，互不影响。

## 5.2 澄清提问（AskUserQuestion 风格）

关键槽位缺失，或用户原文提到返程/酒店/交通模式但字段不确定时，任务进入
`NEEDS_CLARIFICATION`，响应含：

- `clarification_question`：人类可读选项列表  
- `clarification_questions`：结构化问题 + options（label/description/value）  
- `uncertain_slots`：如 `return_trip` / `hotel_need` / `transport_mode`  

用户可用选项字母（A/B）、标签（「只要去程」）或自然语言回复 `POST .../messages`。

## 6. 配置入库（Phase C）

```bash
export DATABASE_URL=...
alembic upgrade head

# 导入并激活
.venv/bin/python examples/import_policy_config.py --file config/travel-policy.json

# 运行时从 PG 读 active 配置
export POLICY_CONFIG_BACKEND=postgres
# 若库中尚无 active，可设 BOOTSTRAP 自动导入一次
export POLICY_CONFIG_BOOTSTRAP_FILE=config/travel-policy.json
uvicorn corporate_travel_agent.api.main:app
```

任务仍内嵌员工快照；历史任务不随配置变更回写。

## 6.1 差旅观察队列（迁移 0012）

`trips` 表多了 `next_check_at`（投影自载荷里 `watch.next_check_at`）、`watch_lease_owner`、
`watch_lease_until`，索引 `(status, next_check_at)`。watch worker 领取
`status IN (BOOKED, REBOOKED) AND next_check_at <= now AND (租约空 OR 已过期)` 的行，Postgres 上
`FOR UPDATE SKIP LOCKED`，写租约；每次 `save()` 的更新语句重算 `next_check_at` 并清租约。
`examples/run_trip_watch_worker.py` 单独跑一个进程，或 `PROCESS_ROLE=worker/all` 在进程内起线程。
迁移前登记的观察对象 `next_check_at` 为空，不会被领取——它们不在动态源接入之前。

## 7. Outbox（Phase D）

表 `outbox_events`：append-only 业务侧事件，供通知/对账 worker 消费。

```python
from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore

store.enqueue(
    aggregate_type="trip_task",
    aggregate_id=task_id,
    event_type="APPROVAL_REQUESTED",
    payload={"approver_id": "..."},
)
for event in store.list_unpublished(limit=50):
    # deliver...
    store.mark_published(event.event_id)
```

`/health` 暴露 `outbox.unpublished_count`。当前编排层不强制写 outbox，便于逐步接入审批通知。

## 8. 故障排查

| 症状 | 处理 |
|---|---|
| `schema is missing tables` | `alembic upgrade head` |
| `DATABASE_AUTO_CREATE is forbidden` | 关掉 AUTO_CREATE，走迁移 |
| 列表看不到旧任务员工 | 旧行投影为空时摘要会回退读 payload；新写入会填列 |
| 延迟重试不跑 | 查 `state=WAITING_FOR_PROVIDER` 与 `next_retry_at`；看调度线程日志 |
| 配置 backend=postgres 启动失败 | 先 `import_policy_config.py` 或设 `POLICY_CONFIG_BOOTSTRAP_FILE` |

## 8.1 只有 Postgres 才有的测试

`tests/test_postgres_live.py` 在 `TEST_DATABASE_URL` 指向 Postgres 时才跑，**会清空重建那个库**：
`SKIP LOCKED` 双 worker 争抢、联合写入的版本列与载荷一致、下单确认整笔回滚、`trip_id` 投影、
JSONB 旧载荷读取与批量升级。本机：

```bash
docker exec corporate-travel-agent-postgres-1 psql -U travel_agent -d postgres -c "CREATE DATABASE travel_agent_test"
TEST_DATABASE_URL=postgresql+psycopg://travel_agent:travel_agent_dev@127.0.0.1:5432/travel_agent_test \
  PYTHONPATH=src .venv/bin/python -m pytest tests/test_postgres_live.py -q
```

CI 的 `postgres-smoke` 作业在迁移和 `verify_postgres.py` 之后跑它。

## 9. CI 期望

工作流应至少：

1. `pip install -e '.[dev,persistence]'`
2. 对空 SQLite URL：`alembic upgrade head`（或 create_schema 路径的单元测试）
3. `pytest tests/test_persistence.py tests/test_api.py -q`
