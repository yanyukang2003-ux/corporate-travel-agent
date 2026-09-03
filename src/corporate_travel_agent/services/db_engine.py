"""共享 SQLAlchemy Engine 构建与运维辅助（连接池、Alembic 版本、池快照）。"""

from __future__ import annotations

import os
from typing import Any, cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import CursorResult, Engine, Result


def rowcount(result: Result[Any]) -> int:
    """UPDATE/DELETE 影响的行数。

    SQLAlchemy 把它放在 `CursorResult` 上，而 `Session.execute` 的静态返回类型是更宽的
    `Result`；乐观锁按行数判冲突，这里把这一步收成一个有类型的名字。
    """
    return cast(CursorResult[Any], result).rowcount


def database_pool_options(database_url: str) -> dict[str, Any]:
    """根据环境变量与数据库方言构造 create_engine 连接池参数。"""
    options: dict[str, Any] = {
        "pool_pre_ping": _env_bool("DATABASE_POOL_PRE_PING", True),
    }
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        return options

    pool_size = _env_int("DATABASE_POOL_SIZE", 5, minimum=1, maximum=100)
    max_overflow = _env_int("DATABASE_MAX_OVERFLOW", 10, minimum=0, maximum=200)
    pool_timeout = _env_float("DATABASE_POOL_TIMEOUT", 30.0, minimum=1.0, maximum=300.0)
    pool_recycle = _env_int("DATABASE_POOL_RECYCLE", 1800, minimum=60, maximum=86_400)
    options.update(
        {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
            "pool_recycle": pool_recycle,
        }
    )
    return options


def create_database_engine(database_url: str, *, engine: Engine | None = None) -> Engine:
    """创建或复用 SQLAlchemy Engine；未提供 database_url 时抛出 ValueError。"""
    if engine is not None:
        return engine
    if not database_url:
        raise ValueError("database_url is required")
    return create_engine(database_url, **database_pool_options(database_url))


def read_alembic_version(engine: Engine) -> str | None:
    """读取当前 Alembic 修订号；版本表不存在或查询失败时返回 None。"""
    with engine.connect() as connection:
        try:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
        except Exception:
            return None
        return None if row is None else str(row[0])


def engine_pool_snapshot(engine: Engine) -> dict[str, Any]:
    """采集连接池运行时快照，供健康检查与可观测性使用。"""
    pool = engine.pool
    snapshot: dict[str, Any] = {
        "dialect": engine.dialect.name,
        "pool_class": type(pool).__name__,
    }
    for attr, key in (
        ("size", "size"),
        ("checkedin", "checked_in"),
        ("checkedout", "checked_out"),
        ("overflow", "overflow"),
    ):
        method = getattr(pool, attr, None)
        if callable(method):
            try:
                snapshot[key] = method()
            except Exception:
                continue
    return snapshot


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value
