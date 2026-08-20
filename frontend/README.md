# 澄行企业差旅前端

这是企业差旅 Agent 的交互原型，基于 React、TypeScript 与 Vite。当前覆盖：

- 自然语言创建差旅、结构化输入入口；
- 行程方案推荐、合规证据、费用拆分、方案选择与对比；
- 我的差旅任务列表与各阶段状态；
- 经理审批队列、政策例外说明与审批意见；
- 差旅政策版本、规则上限与例外机制；
- 审计事件、库存证据、服务状态与“禁止预订/支付”边界。

“智能规划”页面已经连接 FastAPI：自然语言创建任务、澄清回复、方案列表、政策证据、
方案选择和重新规划均使用真实 API 响应。任务列表、审批、政策和审计页面仍保留演示数据，
便于继续评审其信息架构，后续可逐页替换。

```bash
npm install
npm run dev
```

开发服务器会把 `/api/*` 转发到 `http://127.0.0.1:8000/*`。因此本地联调时先在仓库
根目录启动后端：

```bash
.venv/bin/uvicorn corporate_travel_agent.api.main:app --reload
```

要启用 `.env` 中配置的语言模型和只读库存供应商：

```bash
set -a
. ./.env
set +a
.venv/bin/uvicorn corporate_travel_agent.api.main:app --reload
```

如果 `.env` 配置了 PostgreSQL、但本机数据库尚未运行，可在开发会话中临时增加
`export DATABASE_URL=`，让任务仓库回退到内存。此模式重启后任务会清空。模型调用可能
产生费用；Duffel 使用测试模式、LiteAPI 使用只读沙箱，预订和支付保持禁用。

当后端 `AUTH_ENABLED=false` 时，前端使用开发身份直接进入工作台。当
`AUTH_ENABLED=true` 时，前端展示登录页、保存短期 Bearer 会话，并在 401 或主动退出后
清理本地令牌。认证账户文件和签名密钥配置见根目录 `.env.example`。

角色工作区严格按职责划分：员工使用“智能规划 / 我的差旅 / 差旅政策”；审批人使用
“待我审批 / 团队差旅 / 差旅政策”；管理员使用“任务总览 / 政策管理 / 审计与系统”。
管理员不会看到批准或拒绝入口，审批人不会看到新建差旅入口。

质量检查：

```bash
npm run build
npm run lint
```
