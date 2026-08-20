# D11 真实完整恢复回归失败分析

## 结论

D11 严格失败：3 个逻辑任务完成 3 个，通过 2 个，`pass^3 = 0`。失败发生在
OpenAI HTTP 响应之前的 TLS/传输路径，不是模型输出、结构化解析、Duffel 参数或
业务规则错误。Duffel 的两次瞬时连接失败均由一次显式重试恢复；OpenAI 的 3 次
瞬时连接失败中，1 次重试恢复，另 1 个逻辑任务连续两次失败后按冻结上限停止。

本批没有创建订单、支付或 booking intent。实际模型为 `gpt-5.6-sol`；5 次模型请求、
4 次 Duffel Test Mode 搜索均未超过 6 次上限。已取得响应的模型调用估算费用为
USD 0.0262575，低于 USD 0.15 门禁。

## 断点与时间线

调用路径：

`workflow -> OpenAIResponsesLanguageModel -> OpenAI Python SDK -> httpx/httpcore -> TLS`

`workflow -> DuffelProvider -> httpx/httpcore -> TLS`

| 逻辑任务 | OpenAI | Duffel | 最终状态 |
|---|---|---|---|
| 1 | 两次均在约 5 秒处 `SSLEOFError`，未收到 HTTP 响应 | 未进入 | `NEEDS_STRUCTURED_INPUT`，失败 |
| 2 | 首次 `SSLEOFError`，重试成功 | 首次 `SSLEOFError`，重试成功 | `WAITING_FOR_USER`，通过 |
| 3 | 首次成功 | 首次 `SSLEOFError`，重试成功 | `WAITING_FOR_USER`，通过 |

所有 5 个失败请求都满足：

- `response_received = false`
- `http_status = null`
- `request_id = null`
- `retryable = true`
- 终端异常类型为 `SSLEOFError`

因此断点位于本机网络出口/TLS 隧道到供应商 HTTP 层之间。请求没有到达可返回 HTTP
状态和 request ID 的层级。

## 原因分析

### 已确认事实

- 当前进程配置了 `HTTP_PROXY` 和 `HTTPS_PROXY`，均指向本机回环代理
  `127.0.0.1:7897`；OpenAI 与 Duffel 都不在 `NO_PROXY` 中。
- macOS `scutil --proxy` 没有系统代理条目；Python SDK 通过环境变量使用本地代理。
- 不带 Key、不计费的连通性诊断结果：

| 目标 | 经环境代理 | 绕过环境代理直连 |
|---|---|---|
| `api.openai.com` | TLS 在约 5 秒处断开 | TLS 在约 5 秒处断开 |
| `api.duffel.com` | TLS 成功并收到 HTTP 400 | TLS 在约 5 秒处断开 |

- 同一 OpenAI client 后续能够返回 `gpt-5.6-sol` 响应；同一 Duffel 业务参数在重试后
  能返回 Test Mode 航班库存。

### 最可信推断

本机直连公网路径不可用或被中间网络阻断，因此不能通过关闭 `trust_env` 绕开代理。
本地代理/代理节点对 OpenAI 的 TLS 隧道不稳定，同时 Duffel 的新连接也偶发 TLS EOF。
这是跨两个供应商、同一终端异常、同一约 5 秒断点的共同解释。

### 可排除项

- OpenAI Key 无效、模型无权限或模型名错误：这些会产生 HTTP 4xx；本批成功响应也确认
  了 `gpt-5.6 -> gpt-5.6-sol`。
- OpenAI 结构化输出或 Prompt 错误：失败请求没有模型响应，成功的两轮意图完全一致。
- Duffel Token、请求参数或 Test Mode 业务错误：同一参数重试后成功并归档原始响应。
- 通过代码强制直连即可修复：无 Key 直连诊断对两个域名均失败。

## 恢复与效率评测

grader v3 将“是否恢复成功”和“是否遵守重试契约”分开：

- 3/3 逻辑任务的 LLM/Provider 重试契约均合规；第 1 轮只是重试耗尽，并非策略违规。
- 原报告中 4 次重复工具名全部有 `retry_of` 和允许的 reason code。
- 授权重试调用 4 次；无理由重复调用 0 次；重试开销占全部 9 次工具调用的 44.44%。
- 9/9 外部请求均有轨迹记录；5 次响应前失败没有供应商 Token/账单用量，因此费用仍标记
  为 lower bound，资源完整性门禁继续失败。

## 本轮代码修正

- `model-live-provider-grader-v3`：重试达到上限后仍失败时，任务保持失败，但重试契约可判合规。
- 离线 regrade 从原始轨迹重算逐轮重试契约，不复用旧 grader 的布尔结论。
- 报告新增两侧终端异常聚合、共同异常、响应前失败数和请求轨迹覆盖率。
- 工具效率拆分原始重复工具名、授权重试和无理由重复调用。
- 新增连续 `SSLEOFError` 故障注入测试，验证重试耗尽、错误聚合和资源轨迹记账。

这些修正改善评测准确性，不掩盖真实失败，也没有增加生产重试次数。盲目增加重试只能
拉长尾时延并掩盖代理基础设施问题。

## 下一次真实回归前置条件

1. 在本机代理客户端切换或修复可稳定访问 `api.openai.com:443` 的节点/规则。
2. 先执行不带 Key 的 TLS 预检；要求 OpenAI 与 Duffel 经实际运行路径连续 3 次均能
   完成 TLS 并收到任意 HTTP 响应。
3. 预检通过后，使用同一 D11 冻结数据集重新进行 3 次真实回归；这属于新的计费批次，
   需要新的明确授权。

## 网络预检结果（2026-08-03）

`D11-network-preflight-v1` 已沿真实运行继承的本机代理路径完成：OpenAI
`GET /v1/models` 连续 3 次收到 HTTP 401，Duffel `GET /air/airlines` 连续 3 次收到
HTTP 400；全部 6 个探针的 `curl` 退出码和 TLS 校验结果均为 0。无鉴权请求出现 401/400
不代表业务失败，反而证明请求已穿过 TLS 并到达供应商 HTTP 层。因此上述第 1、2 项前置
条件现已满足，网络路径可以进入一次新的 D11 计费回归；第 3 项仍等待新的明确授权。

逐探针证据见 `network-preflight-20260803.json`。该预检没有读取 Key、没有发起模型推理、
没有创建 Duffel offer/order，也没有下单。
