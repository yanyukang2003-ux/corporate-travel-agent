"""domain.constraints：硬约束与软偏好的受支持字面量集合。"""

from typing import Literal

HardConstraint = Literal[
    "arrive_before_meeting",
    "direct_only",
    "train_only",
    "flight_only",
    "hotel_required",
]
"""硬约束名称（须出现在用户意图或结构化输入中方可生效）。"""

SoftPreference = Literal[
    "avoid_early_departure",
    "hotel_near_client",
    "prefer_train",
    "prefer_flight",
    "compare_train_and_flight",
    "lowest_cost",
    "shortest_duration",
]
"""软偏好名称（影响排序，不单独阻断搜索）。"""

SUPPORTED_HARD_CONSTRAINTS = frozenset(
    {
        "arrive_before_meeting",
        "direct_only",
        "train_only",
        "flight_only",
        "hotel_required",
    }
)
"""当前 V1 允许写入 TripRequest 的硬约束全集。"""

SUPPORTED_SOFT_PREFERENCES = frozenset(
    {
        "avoid_early_departure",
        "hotel_near_client",
        "prefer_train",
        "prefer_flight",
        "compare_train_and_flight",
        "lowest_cost",
        "shortest_duration",
    }
)
"""当前 V1 允许写入 TripRequest 的软偏好全集。"""
