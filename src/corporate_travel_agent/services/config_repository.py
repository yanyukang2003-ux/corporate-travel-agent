"""在 PostgreSQL 中持久化与加载企业差旅策略配置文档。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from corporate_travel_agent.domain.models import EmployeeProfileSnapshot, PolicySnapshot
from corporate_travel_agent.services.db_engine import rowcount
from corporate_travel_agent.services.policy_config import (
    LoadedPolicyConfiguration,
    PolicyConfigurationError,
    _parse_policy_configuration,
)
from corporate_travel_agent.services.sqlalchemy_repository import (
    EmployeeProfileRow,
    PolicyConfigurationRow,
    PolicySnapshotRow,
)

_EMPLOYEE_ADAPTER = TypeAdapter(EmployeeProfileSnapshot)
_POLICY_ADAPTER = TypeAdapter(PolicySnapshot)


class SQLAlchemyConfigRepository:
    """存储已校验的策略配置文档及反规范化快照。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.backend_name = f"sqlalchemy:{engine.dialect.name}"

    def save_loaded_configuration(
        self,
        loaded: LoadedPolicyConfiguration,
        *,
        activate: bool = True,
        config_id: str | None = None,
    ) -> str:
        """持久化已校验配置；相同 sha256 复用既有记录。"""
        existing = self.get_by_sha256(loaded.sha256)
        if existing is not None:
            if activate:
                self._activate(existing)
            return existing

        config_id = config_id or str(uuid4())
        now = datetime.now(UTC)
        payload = json.loads(loaded.config.model_dump_json())
        with Session(self.engine) as session, session.begin():
            if activate:
                session.execute(
                    update(PolicyConfigurationRow).values(is_active=False)
                )
            session.add(
                PolicyConfigurationRow(
                    config_id=config_id,
                    config_version=loaded.config.config_version,
                    active_policy_snapshot_id=loaded.config.active_policy_snapshot_id,
                    source_type=loaded.source_type,
                    source_identifier=loaded.source_identifier,
                    sha256=loaded.sha256,
                    payload=payload,
                    is_active=activate,
                    created_at=now,
                )
            )
            # Flush parent first so child FKs see policy_configurations.config_id.
            # Without relationships, SQLAlchemy may otherwise insert employees first.
            session.flush()
            for employee in loaded.employee_snapshots:
                payload_employee = _EMPLOYEE_ADAPTER.dump_python(employee, mode="json")
                existing_employee = session.get(EmployeeProfileRow, employee.snapshot_id)
                if existing_employee is not None:
                    # Snapshot IDs are stable across config revisions; retarget to new config.
                    existing_employee.employee_id = employee.employee_id
                    existing_employee.profile_version = employee.profile_version
                    existing_employee.manager_id = employee.manager_id
                    existing_employee.level = employee.level
                    existing_employee.department = employee.department
                    existing_employee.home_city = employee.home_city
                    existing_employee.payload = payload_employee
                    existing_employee.config_id = config_id
                    existing_employee.created_at = now
                else:
                    session.add(
                        EmployeeProfileRow(
                            snapshot_id=employee.snapshot_id,
                            employee_id=employee.employee_id,
                            profile_version=employee.profile_version,
                            manager_id=employee.manager_id,
                            level=employee.level,
                            department=employee.department,
                            home_city=employee.home_city,
                            payload=payload_employee,
                            config_id=config_id,
                            created_at=now,
                        )
                    )
            for policy in loaded.policy_snapshots:
                payload_policy = _POLICY_ADAPTER.dump_python(policy, mode="json")
                existing_policy = session.get(PolicySnapshotRow, policy.snapshot_id)
                if existing_policy is not None:
                    existing_policy.policy_version = policy.policy_version
                    existing_policy.content_hash = policy.content_hash or ""
                    existing_policy.payload = payload_policy
                    existing_policy.config_id = config_id
                    existing_policy.created_at = now
                else:
                    session.add(
                        PolicySnapshotRow(
                            snapshot_id=policy.snapshot_id,
                            policy_version=policy.policy_version,
                            content_hash=policy.content_hash or "",
                            payload=payload_policy,
                            config_id=config_id,
                            created_at=now,
                        )
                    )
        return config_id

    def get_by_sha256(self, sha256: str) -> str | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(PolicyConfigurationRow).where(PolicyConfigurationRow.sha256 == sha256)
            )
            return None if row is None else row.config_id

    def load_active(self) -> LoadedPolicyConfiguration | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(PolicyConfigurationRow)
                .where(PolicyConfigurationRow.is_active.is_(True))
                .order_by(PolicyConfigurationRow.created_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            return self._row_to_loaded(row)

    def load_active_or_raise(self) -> LoadedPolicyConfiguration:
        loaded = self.load_active()
        if loaded is None:
            raise PolicyConfigurationError(
                "No active policy configuration is stored; import a config first"
            )
        return loaded

    def _activate(self, config_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(PolicyConfigurationRow).values(is_active=False))
            result = session.execute(
                update(PolicyConfigurationRow)
                .where(PolicyConfigurationRow.config_id == config_id)
                .values(is_active=True)
            )
            if rowcount(result) != 1:
                raise PolicyConfigurationError(f"Unknown policy configuration {config_id}")

    def _row_to_loaded(self, row: PolicyConfigurationRow) -> LoadedPolicyConfiguration:
        raw = json.dumps(row.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        loaded = _parse_policy_configuration(
            raw,
            source_type="postgres",
            source_identifier=f"postgres:{row.config_id}",
        )
        # Preserve DB sha256 / version identity over re-serialized bytes.
        return LoadedPolicyConfiguration(
            config=loaded.config,
            source_type="postgres",
            source_identifier=f"postgres:{row.config_id}",
            sha256=row.sha256,
            employee_snapshots=loaded.employee_snapshots,
            policy_snapshots=loaded.policy_snapshots,
            city_aliases=loaded.city_aliases,
        )


def load_policy_configuration_from_database(engine: Engine) -> LoadedPolicyConfiguration:
    """从数据库加载当前激活的策略配置，不存在则抛错。"""
    return SQLAlchemyConfigRepository(engine).load_active_or_raise()


def import_policy_configuration_to_database(
    engine: Engine,
    loaded: LoadedPolicyConfiguration,
    *,
    activate: bool = True,
) -> str:
    """将已加载的策略配置导入数据库，可选立即激活。"""
    return SQLAlchemyConfigRepository(engine).save_loaded_configuration(
        loaded,
        activate=activate,
    )
