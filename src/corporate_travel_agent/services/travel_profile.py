"""从**已经发生过的行程**看出员工的差旅习惯。

## 这一层为什么值得做

产品设计思路里那句话是这一层的全部理由：**最大的精力应放在上下文数据层，
而不是反复调 prompt。没有这层，agent 只是个换了皮的搜索框，员工用两次就回去用携程了。**

在它之前，排序只认这一轮对话里说出口的偏好。一个每次都坐高铁的人，第十次还是要
再说一遍"优先高铁"，否则系统就把飞机排在前面。系统对他一无所知，用第十次和第一次
一样费劲。

## 数据从哪来

只有一处：**这个系统自己产生的、员工真的去订了的行程**（走到 `HANDED_OFF`，
并且选定了方案）。理由是它是唯一不依赖企业 IT 就能跑通的来源——日历、CRM、HR
都要先谈接口，而"他上次选了哪一条"现在就在任务仓储里。

只认交接完成的，不认"看过就走"的：看过不算数，**去订了才算表态**。

## 三条克制

1. **一次不算习惯。** 少于 `_MIN_TRIPS` 趟就什么都不推断，宁可继续问。
   从一次出行推出一个习惯，然后一直照着排，是最容易让人失去信任的做法。
2. **压倒性多数才算。** 达不到 `_MAJORITY` 的比例就不推断——五次里三次坐高铁
   说明不了什么，那叫随机。
3. **每条推断都要能说出理由。** `ProfilePreference.evidence` 是一句给员工看的人话。
   说不出理由的推断不该存在：一个解释不了的排序，员工不会信第二次。

## 冷启动：组织默认值

新员工一趟历史都没有。这时退一步看**同职级、同常驻城市的同事**怎么选，
产出 `ORG_DEFAULT` 档的偏好——它的权重在 `planning/preferences.py` 里是最低的一档
（0.2），因为它根本不是关于他本人的。有了本人的历史之后，本人的推断会盖过它。

## 它绝对不做的事

**画像不参与可行性、不参与政策判定、不改变哪些方案够格进候选池。** 它只在
「同样合规的几条里先看哪一条」上说话。这条边界有测试盯着
（`tests/test_travel_profile.py::ProfileStaysOutOfTheVerdictTests`）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from corporate_travel_agent.domain.enums import (
    PreferenceOrigin,
    TaskState,
    TransportMode,
)
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    EmployeeTravelProfileSnapshot,
    ProfilePreference,
    TravelOptionVersion,
    TripTask,
)
from corporate_travel_agent.services.audit import stable_hash

#: 少于这么多趟就什么都不推断。三趟是"一次是偶然、两次是巧合、三次才是习惯"，
#: 这是一个写明的选择，不是统计学结论。
_MIN_TRIPS = 3

#: 达不到这个比例就不算习惯。0.7 的意思是三趟里至少三趟、四趟里至少三趟。
_MAJORITY = 0.7

#: 早班的界线，和 `planning/preferences.py` 里的罚分界线是同一个钟点。
#: 两处不一致的话，"系统觉得我讨厌早班"和"系统按早班扣分"就会说的不是同一件事。
_EARLY_DEPARTURE_HOUR = 7

#: 通勤多少分钟以内算"离客户近"，同样和罚分那边对齐。
_NEAR_CLIENT_MINUTES = 30

#: 同一家酒店住过几次才算"还会再住"。两次——第二次是他自己又选了一遍。
_REPEAT_HOTEL_TRIPS = 2


class TripHistoryPort(Protocol):
    """读某位员工**已经订过**的行程。

    只有这一个方法：这一层需要的全部输入就是"他过去选了什么"。
    """

    def completed_trips(self, employee_id: str, *, limit: int = 50) -> Sequence[TripTask]:
        """按时间倒序返回该员工走到交接完成、且选定了方案的任务。"""
        ...

    def peer_completed_trips(
        self, *, level: str, home_city: str, limit: int = 200
    ) -> Sequence[TripTask]:
        """同职级、同常驻城市同事的同类任务；冷启动时用来算组织默认值。"""
        ...


class RepositoryTripHistory:
    """把任务仓储当作历史来源的适配器。

    它做的过滤只有两条：**状态是交接完成**、**确实选定了一条方案**。没选方案的
    任务表达不了任何偏好——那趟出行的选择根本没发生。
    """

    def __init__(self, repository) -> None:
        self._repository = repository

    def completed_trips(self, employee_id: str, *, limit: int = 50) -> Sequence[TripTask]:
        tasks = self._repository.list_by_employee(employee_id, limit=limit * 4)
        return tuple(item for item in tasks if _is_completed(item))[:limit]

    def peer_completed_trips(
        self, *, level: str, home_city: str, limit: int = 200
    ) -> Sequence[TripTask]:
        tasks = self._repository.list_by_state(TaskState.HANDED_OFF.value, limit=limit * 4)
        return tuple(
            item
            for item in tasks
            if _is_completed(item)
            and item.employee.level == level
            and item.employee.home_city == home_city
        )[:limit]


def derive_travel_profile(
    employee: EmployeeProfileSnapshot,
    history: TripHistoryPort,
    *,
    declared: EmployeeTravelProfileSnapshot | None = None,
    history_limit: int = 50,
) -> EmployeeTravelProfileSnapshot:
    """算出这位员工当前的习惯画像。

    三层叠加，**上面的盖住下面的同名项**：

    1. `declared` —— 他自己填的档案（本函数不改动，只是带上）；
    2. 他本人历史行程推出来的 `OBSERVED`；
    3. 同级同城同事推出来的 `ORG_DEFAULT`。

    盖住是按名字盖：同一个偏好只留证据最强的那一条来源，
    不让"你说过"和"我们猜的"在排序里互相加码。
    """
    # 这里再过滤一遍，不是重复劳动：`TripHistoryPort` 是个协议，谁都能实现一个。
    # "只认交接完成且选定了方案"是**这个函数**对"历史"的定义，不能指望每个适配器
    # 都记得替它把关——漏一次，画像就从没去订的行程里推出习惯来。
    own = tuple(
        item
        for item in history.completed_trips(employee.employee_id, limit=history_limit)
        if _is_completed(item)
    )
    observed = _preferences_from(own, PreferenceOrigin.OBSERVED, subject="你")

    org_default: tuple[ProfilePreference, ...] = ()
    if len(own) < _MIN_TRIPS:
        # 只有本人的历史不够时才去看同事。够了就不看——他本人的选择比同级平均值
        # 更能代表他，掺进来只会把已经问出来的东西冲淡。
        peers = tuple(
            item
            for item in history.peer_completed_trips(
                level=employee.level, home_city=employee.home_city
            )
            if _is_completed(item)
        )
        org_default = _preferences_from(
            peers,
            PreferenceOrigin.ORG_DEFAULT,
            subject=f"{employee.level} 同城同事",
        )

    declared_preferences = declared.preferences if declared else ()
    merged = _merge_by_strength(declared_preferences, observed, org_default)
    hotels = _repeat_hotels(own)

    # 快照 ID 里带内容哈希，理由和政策快照那边一样：**同一个 ID 不能对应两份内容**。
    # 员工档案版本不变、但他又订了两趟行程时，画像内容会变——只用 profile_version
    # 命名的话，两份不同的画像会共用一个 ID，"这趟当初按哪一份排的"就答不上来了。
    content = stable_hash(
        {
            "preferences": [
                (item.name, item.origin.value, item.evidence) for item in merged
            ],
            "preferred_hotels": hotels,
            "cost_center": declared.cost_center if declared else None,
        }
    )[:12]

    return EmployeeTravelProfileSnapshot(
        snapshot_id=(
            f"travel-profile-{employee.employee_id}"
            f"-v{employee.profile_version}-{content}"
        ),
        employee_id=employee.employee_id,
        profile_version=employee.profile_version,
        preferences=merged,
        preferred_hotels=hotels,
        cost_center=declared.cost_center if declared else None,
        derived_from_trips=len(own),
    )


def _is_completed(task: TripTask) -> bool:
    return task.state is TaskState.HANDED_OFF and task.selected_option() is not None


def _selected_options(tasks: Sequence[TripTask]) -> tuple[TravelOptionVersion, ...]:
    return tuple(
        option for task in tasks if (option := task.selected_option()) is not None
    )


def _preferences_from(
    tasks: Sequence[TripTask],
    origin: PreferenceOrigin,
    *,
    subject: str,
) -> tuple[ProfilePreference, ...]:
    """从一批已完成行程里读出习惯。样本不够就一条都不推断。"""
    options = _selected_options(tasks)
    if len(options) < _MIN_TRIPS:
        return ()

    found: list[ProfilePreference] = []
    found.extend(_mode_preference(options, origin, subject=subject))
    found.extend(_departure_preference(options, origin, subject=subject))
    found.extend(_lodging_preference(options, origin, subject=subject))
    return tuple(found)


def _mode_preference(
    options: Sequence[TravelOptionVersion],
    origin: PreferenceOrigin,
    *,
    subject: str,
) -> list[ProfilePreference]:
    """坐高铁还是坐飞机。按**航段**数，不按行程数：往返两段都坐高铁是两票。"""
    modes = Counter(leg.mode for option in options for leg in option.legs)
    total = sum(modes.values())
    if total == 0:
        return []
    for mode, name in (
        (TransportMode.TRAIN, "prefer_train"),
        (TransportMode.FLIGHT, "prefer_flight"),
    ):
        count = modes[mode]
        if count / total >= _MAJORITY:
            label = "高铁" if mode is TransportMode.TRAIN else "航班"
            return [
                ProfilePreference(
                    name=name,
                    origin=origin,
                    evidence=(
                        f"{subject}最近 {len(options)} 趟出行的 {total} 段里，"
                        f"有 {count} 段选了{label}"
                    ),
                )
            ]
    return []


def _departure_preference(
    options: Sequence[TravelOptionVersion],
    origin: PreferenceOrigin,
    *,
    subject: str,
) -> list[ProfilePreference]:
    """躲不躲早班。"""
    departures = [leg.depart_at for option in options for leg in option.legs]
    if not departures:
        return []
    late = sum(1 for item in departures if item.hour >= _EARLY_DEPARTURE_HOUR)
    if late / len(departures) < _MAJORITY:
        return []
    return [
        ProfilePreference(
            name="avoid_early_departure",
            origin=origin,
            evidence=(
                f"{subject}最近的 {len(departures)} 段里，"
                f"有 {late} 段是 {_EARLY_DEPARTURE_HOUR} 点以后出发"
            ),
        )
    ]


def _lodging_preference(
    options: Sequence[TravelOptionVersion],
    origin: PreferenceOrigin,
    *,
    subject: str,
) -> list[ProfilePreference]:
    """住得离客户近不近。

    通勤时间未知的住宿**不计入分母**：`COMMUTE_UNKNOWN_MINUTES` 是"供应商证明不了"，
    不是"很远"。把它当远的算，会推出一个来自缺数据的假习惯。
    """
    stays = [
        stay.commute_minutes
        for option in options
        for stay in option.stays
        if stay.commute_minutes != COMMUTE_UNKNOWN_MINUTES
    ]
    if len(stays) < _MIN_TRIPS:
        return []
    near = sum(1 for item in stays if item <= _NEAR_CLIENT_MINUTES)
    if near / len(stays) < _MAJORITY:
        return []
    return [
        ProfilePreference(
            name="hotel_near_client",
            origin=origin,
            evidence=(
                f"{subject}住过的 {len(stays)} 家里，"
                f"有 {near} 家在离客户 {_NEAR_CLIENT_MINUTES} 分钟以内"
            ),
        )
    ]


def _repeat_hotels(tasks: Sequence[TripTask]) -> tuple[str, ...]:
    """住过不止一次的酒店。第二次是他自己又选了一遍，才算数。"""
    names = Counter(
        stay.name for option in _selected_options(tasks) for stay in option.stays
    )
    return tuple(
        sorted(name for name, count in names.items() if count >= _REPEAT_HOTEL_TRIPS)
    )


def _merge_by_strength(
    *groups: Sequence[ProfilePreference],
) -> tuple[ProfilePreference, ...]:
    """同名偏好只留证据最强的一条；顺序按名字，好让快照内容可复现。"""
    strength = {
        PreferenceOrigin.STATED: 3,
        PreferenceOrigin.DECLARED: 2,
        PreferenceOrigin.OBSERVED: 1,
        PreferenceOrigin.ORG_DEFAULT: 0,
    }
    best: dict[str, ProfilePreference] = {}
    for group in groups:
        for item in group:
            existing = best.get(item.name)
            if existing is None or strength[item.origin] > strength[existing.origin]:
                best[item.name] = item
    return tuple(best[name] for name in sorted(best))
