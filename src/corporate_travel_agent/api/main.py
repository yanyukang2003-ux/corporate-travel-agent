"""api.main：生产入口，`uvicorn corporate_travel_agent.api.main:app`。

这里只做一件事：按环境变量 `create_app()`。装配在 `api/runtime.py`，配置在 `api/settings.py`，
路由按资源拆在 `api/routers/`，请求与响应模型在 `api/schemas.py`（ADR-0007）。

模块级名字 `runtime` / `workflow` / `task_repository` / `raw_response_store` /
`policy_configuration` 是给测试和脚本读的兼容入口，都指向 `app.state.runtime` 里的同一批
对象。要换掉某个部件（比如测试里换鉴权服务或整个编排器），改 `runtime` 上的属性，不要重绑
这里的名字——路由只认 `app.state.runtime`。
"""

from __future__ import annotations

from corporate_travel_agent.api.app import create_app
from corporate_travel_agent.api.deps import current_identity

app = create_app()
runtime = app.state.runtime
workflow = runtime.workflow
task_repository = runtime.task_repository
raw_response_store = runtime.raw_response_store
policy_configuration = runtime.policy_configuration

#: 测试用 `app.dependency_overrides[_current_identity]` 换身份；名字保留。
_current_identity = current_identity

__all__ = [
    "app",
    "runtime",
    "workflow",
    "task_repository",
    "raw_response_store",
    "policy_configuration",
]
