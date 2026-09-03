"""应用工厂：`create_app(runtime)` 把装配好的运行时挂到 FastAPI 上并注册路由。

同一个进程可以建多个互不相干的应用（各自的仓储、时钟、鉴权），测试不再需要"先设环境变量
再 import"。生产入口 `api/main.py` 只是 `create_app()` 加几个兼容名字。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from corporate_travel_agent.api.routers import (
    admin,
    approvals,
    auth,
    health,
    policy,
    tasks,
    trips,
)
from corporate_travel_agent.api.runtime import ApiRuntime, build_runtime
from corporate_travel_agent.api.settings import ApiSettings

API_TITLE = "Corporate Travel Planning & Compliance Agent"
API_DESCRIPTION = "V1 只规划与合规校验；不代付、不预订、不退改签。"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动 / 停止后台调度器，并在退出时释放供应商连接与数据库连接池。"""
    runtime: ApiRuntime = app.state.runtime
    runtime.start()
    try:
        yield
    finally:
        runtime.stop()


def create_app(
    runtime: ApiRuntime | None = None,
    *,
    settings: ApiSettings | None = None,
) -> FastAPI:
    """建一个应用。不传 `runtime` 就按 `settings`（不传则读环境变量）装配一套。"""
    if runtime is None:
        runtime = build_runtime(settings or ApiSettings.from_environment())
    app = FastAPI(
        title=API_TITLE,
        version="0.1.0",
        description=API_DESCRIPTION,
        lifespan=_lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.settings.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (health, auth, tasks, approvals, policy, trips, admin):
        app.include_router(module.router)
    return app
