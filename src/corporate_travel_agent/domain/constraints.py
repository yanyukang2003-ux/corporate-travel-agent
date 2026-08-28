"""domain.constraints：硬约束与软偏好的受支持字面量集合，以及它们能管到哪里。

「作用域」就是一句大白话：这条要求是管整趟行程，还是只管其中某一段。
"去程直飞就行、返程无所谓"是完全正常的一句话，可是在只有一个全局字符串
`direct_only` 的模型里根本没法表达——于是它要么被放大到全程，要么被丢掉。
"""

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

WHOLE_JOURNEY_ONLY_REQUIREMENTS = frozenset(
    {
        "hotel_required",
        "hotel_near_client",
        "lowest_cost",
        "shortest_duration",
        "compare_train_and_flight",
    }
)
"""只能作用于整趟行程的名字：把它们绑到某一个航段上没有意义。

"住得离客户近"说的是住宿，不是某一段交通；"最便宜"说的是整趟的总账，
不是某一段的票价。给这些名字标航段号是把话说错了，所以直接判为冲突。
"""

LEG_SCOPABLE_REQUIREMENTS = (
    SUPPORTED_HARD_CONSTRAINTS | SUPPORTED_SOFT_PREFERENCES
) - WHOLE_JOURNEY_ONLY_REQUIREMENTS
"""能绑到某一段的名字：直飞、限定交通方式、避开早班、会前必须到。"""
