"""企业差旅策略配置：严格 JSON schema 校验、加载与环境后端选择。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from corporate_travel_agent.domain.models import (
    ApprovalTier,
    CostCenterBudget,
    EmployeeProfileSnapshot,
    LevelTravelRule,
    PolicySnapshot,
    SeasonalHotelCap,
)
from corporate_travel_agent.services.city_registry import city_registry

MAX_POLICY_CONFIG_BYTES = 1024 * 1024
KNOWN_EXCEPTION_RULE_IDS = frozenset(
    {
        "transport.flight.seat_class",
        "transport.train.seat_class",
        "hotel.city.nightly_cap",
        "hotel.city.seasonal_cap",
        "booking.advance_days",
        "budget.cost_center.remaining",
    }
)

#: 引擎会产出的全部规则 ID：审批链的触发条件只能引用这些。
KNOWN_RULE_IDS = KNOWN_EXCEPTION_RULE_IDS | {
    "employee.level.known",
    "pricing.currency",
    "policy.effective_window",
}


class PolicyConfigurationError(RuntimeError):
    """企业差旅策略配置不可信或无法加载时抛出。"""


class StrictConfigModel(BaseModel):
    """禁止额外字段、严格类型的配置基类。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class CityConfig(StrictConfigModel):
    """城市编码、规范名与别名列表。"""

    code: str = Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{2,8}$")
    canonical_name: str = Field(min_length=1, max_length=100)
    aliases: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("aliases")
    @classmethod
    def aliases_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [value.casefold() for value in values]
        if any(not value for value in values):
            raise ValueError("city aliases cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("city aliases must be unique within a city")
        return values


class ApproverConfig(StrictConfigModel):
    """审批人配置项。"""

    approver_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    display_name: str = Field(min_length=1, max_length=100)


class EmployeeConfig(StrictConfigModel):
    """员工档案配置项（职级、部门、常驻城市、上级等）。"""

    snapshot_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    employee_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    profile_version: int = Field(ge=1, le=1_000_000)
    level: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    department: str = Field(min_length=1, max_length=100)
    home_city_code: str = Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{2,8}$")
    manager_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    #: 成本中心。不填就没有预算规则；填了但政策没给它配预算，同样没有。
    cost_center: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    #: 可以替这位员工订差旅的员工 ID。
    delegates: tuple[str, ...] = Field(default=(), max_length=50)

    @field_validator("delegates")
    @classmethod
    def delegates_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("delegates must be unique")
        return values


def _safe_money(value: Decimal, label: str) -> Decimal:
    if value.is_nan() or value.is_infinite() or value <= 0 or value > Decimal("1000000000"):
        raise ValueError(f"invalid amount for {label}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError(f"amount for {label} has more than two decimals")
    return value


class SeasonalHotelCapConfig(StrictConfigModel):
    """某城市在某日期窗口内的夜费上限（旺季/会展季）。"""

    city_code: str = Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{2,8}$")
    label: str = Field(min_length=1, max_length=60)
    season_from: date
    season_to: date
    nightly_cap: Decimal

    @field_validator("nightly_cap")
    @classmethod
    def cap_is_safe(cls, value: Decimal) -> Decimal:
        return _safe_money(value, "seasonal hotel cap")

    @model_validator(mode="after")
    def window_is_ordered(self) -> SeasonalHotelCapConfig:
        if self.season_to < self.season_from:
            raise ValueError("season_to cannot be earlier than season_from")
        return self


class CostCenterBudgetConfig(StrictConfigModel):
    """一个成本中心在一个预算期内的差旅预算上限；币种跟政策快照走。"""

    cost_center: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    amount: Decimal
    period_from: date
    period_to: date

    @field_validator("amount")
    @classmethod
    def amount_is_safe(cls, value: Decimal) -> Decimal:
        return _safe_money(value, "cost center budget")

    @model_validator(mode="after")
    def period_is_ordered(self) -> CostCenterBudgetConfig:
        if self.period_to < self.period_from:
            raise ValueError("period_to cannot be earlier than period_from")
        return self


class ApprovalTierConfig(StrictConfigModel):
    """直属经理之后追加的一级审批：什么情况下要多一个人批、由谁批。"""

    label: str = Field(min_length=1, max_length=60)
    approver_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    above_amount: Decimal | None = None
    when_rules: tuple[str, ...] = ()

    @field_validator("above_amount")
    @classmethod
    def amount_is_safe(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if value.is_nan() or value.is_infinite() or value < 0 or value > Decimal("1000000000"):
            raise ValueError("invalid approval tier amount")
        return value

    @field_validator("when_rules")
    @classmethod
    def rules_are_known_and_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(values) - KNOWN_RULE_IDS
        if unknown:
            raise ValueError(f"unknown approval tier rule IDs: {', '.join(sorted(unknown))}")
        # 排序去重：内容哈希要在进程之间稳定，集合的顺序不能进哈希。
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def has_a_trigger(self) -> ApprovalTierConfig:
        if self.above_amount is None and not self.when_rules:
            raise ValueError("an approval tier needs above_amount or when_rules")
        return self


class LevelTravelRuleConfig(StrictConfigModel):
    """职级对应的舱位/座席许可规则。"""

    allowed_flight_classes: tuple[str, ...] = Field(min_length=1, max_length=20)
    allowed_train_classes: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("allowed_flight_classes", "allowed_train_classes")
    @classmethod
    def classes_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", value) is None
            for value in values
        ):
            raise ValueError("travel classes must be non-empty uppercase identifiers")
        if len(values) != len(set(values)):
            raise ValueError("travel classes must be unique")
        return values


class PolicySnapshotConfig(StrictConfigModel):
    """单份策略快照的配置形态（有效期、职级规则、酒店城市上限等）。"""

    snapshot_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    policy_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    effective_from: date
    effective_to: date | None = None
    arrival_buffer_minutes: int = Field(ge=0, le=1440)
    level_rules: dict[str, LevelTravelRuleConfig] = Field(min_length=1, max_length=100)
    hotel_city_caps: dict[str, Decimal] = Field(default_factory=dict, max_length=1000)
    exception_allowed_rule_ids: frozenset[str] = Field(default_factory=frozenset)
    # 后加的三个维度。都是可选的：旧配置一个字不改仍然合法，内容哈希也不变
    # （见 `_policy_content_hash`）——这是"配置升级只追加快照"那条规矩的前提。
    min_advance_booking_days: int | None = Field(default=None, ge=0, le=365)
    hotel_seasonal_caps: tuple[SeasonalHotelCapConfig, ...] = Field(default=(), max_length=1000)
    cost_center_budgets: tuple[CostCenterBudgetConfig, ...] = Field(default=(), max_length=10000)
    approval_tiers: tuple[ApprovalTierConfig, ...] = Field(default=(), max_length=20)

    @field_validator("cost_center_budgets")
    @classmethod
    def budgets_name_distinct_cost_centers(
        cls, values: tuple[CostCenterBudgetConfig, ...]
    ) -> tuple[CostCenterBudgetConfig, ...]:
        names = [item.cost_center for item in values]
        if len(names) != len(set(names)):
            raise ValueError("each cost center may have at most one budget per policy")
        return values

    @field_validator("level_rules")
    @classmethod
    def levels_are_normalized(
        cls, values: dict[str, LevelTravelRuleConfig]
    ) -> dict[str, LevelTravelRuleConfig]:
        for level in values:
            if re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", level) is None:
                raise ValueError("level rule keys must be uppercase identifiers")
        return values

    @field_validator("hotel_city_caps")
    @classmethod
    def hotel_caps_are_safe(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        for city_code, cap in values.items():
            if cap.is_nan() or cap.is_infinite() or cap <= 0 or cap > Decimal("1000000"):
                raise ValueError(f"invalid hotel cap for {city_code}")
            exponent = cap.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -2:
                raise ValueError(f"hotel cap for {city_code} has more than two decimals")
        return values

    @field_validator("exception_allowed_rule_ids")
    @classmethod
    def exceptions_are_known(cls, values: frozenset[str]) -> frozenset[str]:
        unknown = values - KNOWN_EXCEPTION_RULE_IDS
        if unknown:
            raise ValueError(f"unknown exception rule IDs: {', '.join(sorted(unknown))}")
        return values

    @model_validator(mode="after")
    def dates_are_ordered(self) -> PolicySnapshotConfig:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be earlier than effective_from")
        return self


class EnterpriseTravelPolicyConfig(StrictConfigModel):
    """完整企业差旅策略配置文档（城市、审批人、员工、策略快照）。"""

    schema_version: Literal[1]
    config_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    timezone_name: str = Field(min_length=1, max_length=100)
    active_policy_snapshot_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    cities: tuple[CityConfig, ...] = Field(min_length=1, max_length=1000)
    approvers: tuple[ApproverConfig, ...] = Field(min_length=1, max_length=1000)
    employees: tuple[EmployeeConfig, ...] = Field(min_length=1, max_length=100_000)
    policies: tuple[PolicySnapshotConfig, ...] = Field(min_length=1, max_length=100)

    @field_validator("timezone_name")
    @classmethod
    def timezone_is_known(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def references_are_consistent(self) -> EnterpriseTravelPolicyConfig:
        city_codes = _unique_values(self.cities, "code", "city code")
        canonical_names = _unique_casefold_values(
            self.cities, "canonical_name", "canonical city name"
        )
        _ = canonical_names

        aliases: dict[str, str] = {}
        for city in self.cities:
            for alias in (*city.aliases, city.canonical_name, city.code):
                normalized = alias.casefold()
                existing = aliases.get(normalized)
                if existing is not None and existing != city.code:
                    raise ValueError(f"city alias {alias!r} maps to multiple cities")
                aliases[normalized] = city.code

        approver_ids = _unique_values(self.approvers, "approver_id", "approver ID")
        employee_ids = _unique_values(self.employees, "employee_id", "employee ID")
        _unique_values(self.employees, "snapshot_id", "employee snapshot ID")
        policy_ids = _unique_values(self.policies, "snapshot_id", "policy snapshot ID")
        _unique_values(self.policies, "policy_version", "policy version")

        if self.active_policy_snapshot_id not in policy_ids:
            raise ValueError("active_policy_snapshot_id does not reference a configured policy")
        active_policy = next(
            policy
            for policy in self.policies
            if policy.snapshot_id == self.active_policy_snapshot_id
        )

        for employee in self.employees:
            if employee.home_city_code not in city_codes:
                raise ValueError(
                    f"employee {employee.employee_id} references an unknown home city"
                )
            if employee.manager_id not in approver_ids:
                raise ValueError(
                    f"employee {employee.employee_id} references an unknown approver"
                )
            if employee.employee_id == employee.manager_id:
                raise ValueError(f"employee {employee.employee_id} cannot approve their own trip")
            if employee.employee_id in employee.delegates:
                raise ValueError(f"employee {employee.employee_id} cannot delegate to themselves")
            unknown_delegates = set(employee.delegates) - employee_ids
            if unknown_delegates:
                raise ValueError(
                    f"employee {employee.employee_id} lists unknown delegates: "
                    + ", ".join(sorted(unknown_delegates))
                )
            if employee.level not in active_policy.level_rules:
                raise ValueError(
                    f"employee {employee.employee_id} has no rule in the active policy"
                )

        if employee_ids & approver_ids:
            raise ValueError("employee IDs and approver IDs must be distinct")
        for policy in self.policies:
            unknown_cities = set(policy.hotel_city_caps) - city_codes
            if unknown_cities:
                raise ValueError(
                    "hotel caps reference unknown city codes: "
                    + ", ".join(sorted(unknown_cities))
                )
            unknown_seasonal = {
                item.city_code for item in policy.hotel_seasonal_caps
            } - city_codes
            if unknown_seasonal:
                raise ValueError(
                    "seasonal hotel caps reference unknown city codes: "
                    + ", ".join(sorted(unknown_seasonal))
                )
            unknown_tier_approvers = {
                item.approver_id for item in policy.approval_tiers
            } - approver_ids
            if unknown_tier_approvers:
                raise ValueError(
                    "approval tiers reference unknown approvers: "
                    + ", ".join(sorted(unknown_tier_approvers))
                )
        return self


@dataclass(frozen=True, slots=True)
class LoadedPolicyConfiguration:
    """已解析并派生出领域快照的策略配置加载结果。"""

    config: EnterpriseTravelPolicyConfig
    source_type: Literal["bundled", "external", "postgres"]
    source_identifier: str
    sha256: str
    employee_snapshots: tuple[EmployeeProfileSnapshot, ...]
    policy_snapshots: tuple[PolicySnapshot, ...]
    city_aliases: dict[str, str]

    @property
    def active_policy(self) -> PolicySnapshot:
        """返回当前激活的策略快照。"""
        return next(
            policy
            for policy in self.policy_snapshots
            if policy.snapshot_id == self.config.active_policy_snapshot_id
        )


def load_policy_configuration(
    path: str | os.PathLike[str] | None = None,
) -> LoadedPolicyConfiguration:
    """从打包默认文件或外部路径加载并校验策略配置。"""
    if path is None:
        resource = files("corporate_travel_agent").joinpath("config/default_policy.json")
        try:
            raw = resource.read_bytes()
        except OSError as exc:
            raise PolicyConfigurationError("Bundled policy configuration cannot be read") from exc
        return _parse_policy_configuration(
            raw,
            source_type="bundled",
            source_identifier="corporate_travel_agent/config/default_policy.json",
        )

    config_path = Path(path).expanduser()
    try:
        if not config_path.is_file():
            raise PolicyConfigurationError(
                f"POLICY_CONFIG_FILE is not a regular file: {config_path}"
            )
        size = config_path.stat().st_size
        if size > MAX_POLICY_CONFIG_BYTES:
            raise PolicyConfigurationError(
                f"Policy configuration exceeds {MAX_POLICY_CONFIG_BYTES} bytes"
            )
        raw = config_path.read_bytes()
    except PolicyConfigurationError:
        raise
    except OSError as exc:
        raise PolicyConfigurationError(
            f"Policy configuration cannot be read: {config_path}"
        ) from exc
    return _parse_policy_configuration(
        raw,
        source_type="external",
        source_identifier=str(config_path.resolve()),
    )


def load_policy_configuration_from_environment() -> LoadedPolicyConfiguration:
    """按环境变量从文件或 PostgreSQL 加载策略配置。

    环境变量：
      POLICY_CONFIG_BACKEND=file|postgres  （默认 file）
      POLICY_CONFIG_FILE=...               backend=file 时的路径
      DATABASE_URL=...                     backend=postgres 时必需
      POLICY_CONFIG_BOOTSTRAP_FILE=...     可选：无激活行时导入到 PG
    """
    backend = (os.getenv("POLICY_CONFIG_BACKEND") or "file").casefold()
    if backend in {"file", "json", "bundled"}:
        return load_policy_configuration(os.getenv("POLICY_CONFIG_FILE") or None)
    if backend in {"postgres", "postgresql", "db"}:
        return _load_policy_configuration_from_postgres()
    raise PolicyConfigurationError(
        "POLICY_CONFIG_BACKEND must be 'file' or 'postgres'"
    )


def _load_policy_configuration_from_postgres() -> LoadedPolicyConfiguration:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise PolicyConfigurationError(
            "POLICY_CONFIG_BACKEND=postgres requires DATABASE_URL"
        )
    try:
        from corporate_travel_agent.services.config_repository import (
            SQLAlchemyConfigRepository,
            import_policy_configuration_to_database,
        )
        from corporate_travel_agent.services.db_engine import create_database_engine
    except ImportError as exc:
        raise PolicyConfigurationError(
            "PostgreSQL policy config requires the persistence extra"
        ) from exc

    engine = create_database_engine(database_url)
    try:
        repository = SQLAlchemyConfigRepository(engine)
        loaded = repository.load_active()
        if loaded is not None:
            return loaded
        bootstrap = os.getenv("POLICY_CONFIG_BOOTSTRAP_FILE")
        if not bootstrap:
            raise PolicyConfigurationError(
                "No active policy configuration in PostgreSQL; set "
                "POLICY_CONFIG_BOOTSTRAP_FILE to import once, or insert a config"
            )
        seeded = load_policy_configuration(bootstrap)
        import_policy_configuration_to_database(engine, seeded, activate=True)
        return repository.load_active_or_raise()
    finally:
        engine.dispose()


def _parse_policy_configuration(
    raw: bytes,
    *,
    source_type: Literal["bundled", "external", "postgres"],
    source_identifier: str,
) -> LoadedPolicyConfiguration:
    if len(raw) > MAX_POLICY_CONFIG_BYTES:
        raise PolicyConfigurationError(
            f"Policy configuration exceeds {MAX_POLICY_CONFIG_BYTES} bytes"
        )
    try:
        config = EnterpriseTravelPolicyConfig.model_validate_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise PolicyConfigurationError(f"Invalid policy configuration: {exc}") from exc

    cities = {city.code: city.canonical_name for city in config.cities}
    # 别名先取城市登记表（`config/cities.json`，地理事实），再让这份政策配置覆盖它。
    # **顺序是有意的**：登记表管"世界上有哪些城市、都叫什么"，政策配置管"这家公司
    # 怎么称呼它们"——同名时以部署方的配置为准，但部署方不写也不会因此少认一座城市。
    city_aliases = dict(city_registry().alias_map())
    city_aliases.update(
        {
            alias: city.canonical_name
            for city in config.cities
            for alias in (*city.aliases, city.canonical_name, city.code)
        }
    )
    employees = tuple(
        EmployeeProfileSnapshot(
            snapshot_id=employee.snapshot_id,
            employee_id=employee.employee_id,
            level=employee.level,
            department=employee.department,
            home_city=cities[employee.home_city_code],
            manager_id=employee.manager_id,
            profile_version=employee.profile_version,
            cost_center=employee.cost_center,
            delegate_ids=tuple(employee.delegates),
        )
        for employee in config.employees
    )
    policies = tuple(
        PolicySnapshot(
            snapshot_id=policy.snapshot_id,
            policy_version=policy.policy_version,
            level_rules={
                level: LevelTravelRule(
                    allowed_flight_classes=rule.allowed_flight_classes,
                    allowed_train_classes=rule.allowed_train_classes,
                )
                for level, rule in policy.level_rules.items()
            },
            hotel_city_caps={
                cities[city_code]: cap
                for city_code, cap in policy.hotel_city_caps.items()
            },
            arrival_buffer_minutes=policy.arrival_buffer_minutes,
            exception_allowed_rule_ids=policy.exception_allowed_rule_ids,
            effective_from=policy.effective_from,
            effective_to=policy.effective_to,
            content_hash=_policy_content_hash(policy),
            currency=policy.currency,
            min_advance_booking_days=policy.min_advance_booking_days,
            hotel_seasonal_caps=tuple(
                SeasonalHotelCap(
                    city=cities[item.city_code],
                    season_from=item.season_from,
                    season_to=item.season_to,
                    nightly_cap=item.nightly_cap,
                    label=item.label,
                )
                for item in policy.hotel_seasonal_caps
            ),
            cost_center_budgets={
                item.cost_center: CostCenterBudget(
                    cost_center=item.cost_center,
                    amount=item.amount,
                    period_from=item.period_from,
                    period_to=item.period_to,
                    currency=policy.currency,
                )
                for item in policy.cost_center_budgets
            },
            approval_tiers=tuple(
                ApprovalTier(
                    label=item.label,
                    approver_id=item.approver_id,
                    above_amount=item.above_amount,
                    when_rules=frozenset(item.when_rules),
                )
                for item in policy.approval_tiers
            ),
        )
        for policy in config.policies
    )
    return LoadedPolicyConfiguration(
        config=config,
        source_type=source_type,
        source_identifier=source_identifier,
        sha256=hashlib.sha256(raw).hexdigest(),
        employee_snapshots=employees,
        policy_snapshots=policies,
        city_aliases=city_aliases,
    )


def _unique_values(items: tuple[Any, ...], attribute: str, label: str) -> set[str]:
    values = [getattr(item, attribute) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")
    return set(values)


def _unique_casefold_values(items: tuple[Any, ...], attribute: str, label: str) -> set[str]:
    values = [getattr(item, attribute).casefold() for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")
    return set(values)


def _policy_content_hash(policy: PolicySnapshotConfig) -> str:
    payload = policy.model_dump(mode="json")
    # Currency was added after schema v1 tasks already existed. An omitted currency
    # retains the legacy CNY semantics and legacy hash; an explicitly configured
    # currency is part of the immutable policy content.
    if "currency" not in policy.model_fields_set:
        payload.pop("currency", None)
    # 同样的道理，后加的三个维度只有**写了**才进哈希：没写的旧快照哈希一位不变，
    # 否则升级代码那一刻所有在途任务都会被"政策内容变了"拦下来。
    for name in (
        "min_advance_booking_days",
        "hotel_seasonal_caps",
        "cost_center_budgets",
        "approval_tiers",
    ):
        if name not in policy.model_fields_set:
            payload.pop(name, None)
    canonical = json.dumps(
        _canonicalize_sets(policy, payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _canonicalize_sets(policy: PolicySnapshotConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """把集合字段序列化出的列表排序，让哈希在进程之间稳定。

    集合没有顺序，Python 每个进程的字符串哈希种子又不同，所以 `model_dump` 出来的
    列表顺序每次启动都可能不一样。`sort_keys=True` 只排字典的**键**，不排列表的**值**，
    于是同一份政策会算出不同的哈希——重启之后所有在途任务都会被
    "Historical policy snapshot content has changed" 拦下来，而政策其实一个字没改。
    """
    canonical = dict(payload)
    for name in type(policy).model_fields:
        if name not in canonical:
            continue
        if isinstance(getattr(policy, name, None), (set, frozenset)):
            value = canonical[name]
            if isinstance(value, list):
                canonical[name] = sorted(value)
    return canonical
