"""部署关键运行时配置的 fail-closed 校验：生产类环境必须显式开启安全依赖。"""

from __future__ import annotations

from collections.abc import Mapping

_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod", "staging"})


def validate_deployment_environment(environment: Mapping[str, str]) -> None:
    """校验部署环境变量；非开发环境缺少数据库/鉴权/原响应存储时直接拒绝启动。

    开发环境保留零配置演示体验；production/staging 必须显式配置，
    避免缺失环境变量却仍呈现「看似健康」的未鉴权内存部署。
    """

    profile = environment.get("ENVIRONMENT", "development").strip().casefold()
    if profile not in _PRODUCTION_ENVIRONMENTS:
        return

    missing: list[str] = []
    if not environment.get("DATABASE_URL", "").strip():
        missing.append("DATABASE_URL")
    if environment.get("AUTH_ENABLED", "false").strip().casefold() != "true":
        missing.append("AUTH_ENABLED=true")
    if not environment.get("AUTH_USERS_FILE", "").strip():
        missing.append("AUTH_USERS_FILE")
    if not environment.get("AUTH_SIGNING_SECRET", "").strip():
        missing.append("AUTH_SIGNING_SECRET")
    if not environment.get("RAW_RESPONSE_STORE_DIR", "").strip():
        missing.append("RAW_RESPONSE_STORE_DIR")

    if missing:
        raise RuntimeError(
            f"ENVIRONMENT={profile} requires fail-closed runtime configuration: "
            + ", ".join(missing)
        )
