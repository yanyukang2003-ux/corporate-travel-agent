"""行程规划器：先枚举**走法**，再为每种走法挑报价，最后按类别与政策分档摆出来。

**走法**：这趟差旅**怎么走**——每一段坐飞机还是高铁、要不要住一晚。
14 点那班和 16 点那班不是两种走法，是同一种走法的两个价格。

此前这里是「拿到全部报价 → 笛卡尔积 → 过滤 → 按分数取前三」，于是推荐列表经常
是同一个走法的三个价格：看着有三个选择，其实只有一个。差旅本来就没有唯一的最好
——便宜的起得早，舒服的贵——把它们揉成一个分数再取前三，等于替旅行者做了他自己
该做的取舍。规划器该产出的是**几个真正不同的走法，每个都是它那一类里最好的**。

规模在开工前量过（`examples/measure_plan_shape_enumeration.py`）：6 段行程最坏 128
种走法，库存查询次数一次不多——一段查一次就把这段所有交通方式都拿回来了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from heapq import heappop, heappush
from itertools import product

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    EmployeeTravelProfileSnapshot,
    HotelOffer,
    PolicyDecision,
    PolicySnapshot,
    TransportOffer,
    TravelOptionVersion,
    TripRequestVersion,
)
from corporate_travel_agent.policy.engine import PolicyEngine, unreviewable_reasons
from corporate_travel_agent.policy.gaps import unjudged_gap_sentences

from .feasibility import FeasibilityValidator, planned_leg_count
from .preferences import (
    duration_minutes_per_unit,
    leg_penalty,
    lodging_penalty,
    preference_penalty,
    wants_mode_comparison,
)


@dataclass(frozen=True, slots=True)
class PlanShape:
    """一种走法：每段坐什么，住不住。"""

    modes: tuple[TransportMode, ...]
    with_lodging: bool

    def label(self) -> str:
        """给解释用的一句话标签，例如 ``FLIGHT+TRAIN+hotel``。"""
        parts = [mode.value for mode in self.modes]
        if self.with_lodging:
            parts.append("hotel")
        return "+".join(parts)


def _leg_satisfies_constraints(
    request: TripRequestVersion, leg_index: int, offer: TransportOffer
) -> bool:
    """这一段受哪几条硬要求管，就只用那几条判它。

    此前 `direct_only` 是一个全局字符串，"去程直飞就行、返程无所谓"只能被放大成
    全程直飞，于是旅行者明明接受的返程方案被无声筛掉了。
    """
    names = request.constraints_for_leg(leg_index)
    if "train_only" in names and offer.mode is not TransportMode.TRAIN:
        return False
    if "flight_only" in names and offer.mode is not TransportMode.FLIGHT:
        return False
    return not ("direct_only" in names and not offer.is_direct)


@dataclass(frozen=True, slots=True)
class _Choice:
    """一段交通（或一次住宿）的一个候选，排序要用的几个量先算好。

    ``band`` 是**这一项自己**的政策档。政策证据本来就是逐项产生的——每段各判舱位、
    币种与生效窗口，住宿判城市上限——最后由 `PolicyEngine._aggregate` 取最差的一档。
    所以"整条方案不超过某一档"和"每一项都不超过那一档"是同一件事，
    这条等式是 `_shape_candidates` 能按段各取最优的前提。
    """

    offer: TransportOffer | HotelOffer | None
    key: str
    #: 摆出来长什么样。两个候选这一项相同，就是同一条方案的两种写法——
    #: 和 `_display_fingerprint` 用的是同一条规则。
    display_key: tuple[object, ...]
    mode: TransportMode | None
    band: int
    score: Decimal
    price: Decimal
    duration: int


#: "不住"这一个候选：不贡献价格、时长，也不贡献任何政策证据。
_NO_LODGING = _Choice(
    offer=None,
    key="",
    display_key=(),
    mode=None,
    band=0,
    score=Decimal("0"),
    price=Decimal("0"),
    duration=0,
)


def _distinct_by_display(choices: list[_Choice]) -> list[_Choice]:
    """同一轴上"摆出来一模一样"的候选只留最好的一个。

    三家名字、价格、住期都相同的酒店在结果里本来就会被 `_display_fingerprint`
    合成一条。留着它们，按名次取前几组时会把名额全耗在这组重复上——
    真正该被摆出来的第二个航班反而挤不进来。先收掉，名次才落在有意义的差别上。
    """
    best: dict[tuple[object, ...], _Choice] = {}
    for choice in choices:
        current = best.get(choice.display_key)
        if current is None or (choice.band, choice.score, choice.key) < (
            current.band,
            current.score,
            current.key,
        ):
            best[choice.display_key] = choice
    return list(best.values())


class ItineraryPlanner:
    """先枚举走法，再为每种走法挑报价，最后按类别与政策分档选出要摆的方案。"""

    def __init__(
        self,
        validator: FeasibilityValidator | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.validator = validator or FeasibilityValidator()
        self.policy_engine = policy_engine or PolicyEngine()

    def plan(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        leg_offers: Sequence[Sequence[TransportOffer]],
        hotel_offers: Sequence[HotelOffer] | Sequence[Sequence[HotelOffer]],
        limit: int = 3,
        *,
        journey_fares: Sequence[Sequence[TransportOffer]] = (),
        profile: EmployeeTravelProfileSnapshot | None = None,
        now: datetime,
    ) -> list[TravelOptionVersion]:
        """产出最多 ``limit`` 条**互不相同的**方案，外加需要说明的被挡方案。

        ``leg_offers`` 按航段顺序给：第 i 项是第 i 段的报价。段数由请求自己说了算
        （`planned_leg_count`），少给的段按"没货"处理。此前这里是
        ``outbound_offers`` / ``inbound_offers`` 两个参数——两个位置，放不下第三段。

        ``hotel_offers`` 同理按**住宿站**给：第 i 项是第 i 站的酒店。
        只给一串酒店（不分站）时按"只有一站"理解，和改动之前逐字一致。

        ``journey_fares`` 是**整票**：一项就是一张覆盖全部航段的票，按航段顺序给出
        它的每一段。整票和分段购买是两种**真正不同的走法**（§30.2），所以两边的
        方案一起摆出来、由人取舍——不是二选一。整票便宜（实测 15%–76%），
        分段可以各段单独退改，这个取舍不该由规划器替旅行者做。
        """
        pools = [
            [
                offer
                for offer in (leg_offers[index] if index < len(leg_offers) else ())
                if _leg_satisfies_constraints(request, index, offer)
            ]
            for index in range(planned_leg_count(request))
        ]
        stay_pools = _stay_pools(request, hotel_offers)

        minutes_per_unit = duration_minutes_per_unit(request)
        # 先把每一段（以及每一站住宿）各自的候选算好：可行性、政策档、分数贡献都
        # 只看这一项，所以整个行程只算一遍，各走法共用。
        leg_axes = [
            self._leg_choices(
                request, employee, policy, index, pool, minutes_per_unit,
                profile=profile, now=now,
            )
            for index, pool in enumerate(pools)
        ]
        stay_axes = [
            self._lodging_choices(request, employee, policy, index, pool, profile=profile)
            for index, pool in enumerate(stay_pools)
        ]

        candidates: list[tuple[PlanShape, TravelOptionVersion]] = []
        candidates.extend(
            self._fare_candidates(
                request,
                employee,
                policy,
                journey_fares,
                stay_axes,
                minutes_per_unit,
                limit,
                profile=profile,
                now=now,
            )
        )
        for shape in _enumerate_shapes(pools, stay_pools):
            candidates.extend(
                self._shape_candidates(
                    request,
                    employee,
                    policy,
                    shape,
                    leg_axes,
                    stay_axes,
                    minutes_per_unit,
                    limit,
                    profile=profile,
                    now=now,
                )
            )

        return _select_options(
            candidates, limit, compare_modes=wants_mode_comparison(request)
        )

    def _fare_candidates(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        journey_fares: Sequence[Sequence[TransportOffer]],
        stay_axes: list[list[_Choice]],
        minutes_per_unit: Decimal,
        limit: int,
        *,
        profile: EmployeeTravelProfileSnapshot | None = None,
        now: datetime,
    ) -> list[tuple[PlanShape, TravelOptionVersion]]:
        """整票各自评成方案。**一张整票是一个不可拆的候选。**

        这是整票和分段购买最要紧的区别，也是它没法走上面那条路的原因：
        `_shape_candidates` 把每段当成一根独立的轴、各取最优再组装，
        而整票的几段**不能拆开、也不能和别的票的段混搭**——它们是一件商品。
        所以这里不做组合，只把每张票原样评一遍，再配上住宿。

        §32.3 那三条性质（逐项相加、逐项独立、逐项取最差）在住宿这几根轴上仍然成立，
        所以住宿照旧按档各取最优；被固定住的只有交通那部分。
        """
        results: list[tuple[PlanShape, TravelOptionVersion]] = []
        expected = planned_leg_count(request)
        stay_choices = [_distinct_by_display(list(axis)) for axis in stay_axes]
        for fare in journey_fares:
            legs = list(fare)
            if len(legs) != expected:
                # 段数对不上的票不是这趟行程的票，直接不看。
                continue
            if not all(
                _leg_satisfies_constraints(request, index, offer)
                for index, offer in enumerate(legs)
            ):
                continue
            shape = PlanShape(
                modes=tuple(offer.mode for offer in legs),
                with_lodging=bool(stay_choices),
            )
            combos: dict[tuple[str, ...], tuple[_Choice, ...]] = {}
            if stay_choices:
                for ceiling in _POLICY_CEILINGS:
                    limited = [
                        [item for item in axis if item.band <= ceiling]
                        for axis in stay_choices
                    ]
                    if any(not axis for axis in limited):
                        continue
                    for combination in _representative_combinations(limited, limit):
                        combos.setdefault(
                            tuple(item.key for item in combination), combination
                        )
            else:
                combos[()] = ()
            for combination in combos.values():
                option = self._evaluate(
                    request,
                    employee,
                    policy,
                    legs,
                    [item.offer for item in combination if item.offer is not None],
                    shape,
                    minutes_per_unit,
                    profile=profile,
                    now=now,
                )
                if option is not None:
                    results.append((shape, option))
        return results

    def _leg_choices(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        leg_index: int,
        pool: list[TransportOffer],
        minutes_per_unit: Decimal,
        *,
        profile: EmployeeTravelProfileSnapshot | None = None,
        now: datetime,
    ) -> list[_Choice]:
        """这一段自己站得住的报价，外加排序要用的几个量。"""
        choices: list[_Choice] = []
        for offer in pool:
            if self.validator.leg_reasons(
                request, leg_index, offer, policy.arrival_buffer_minutes, now=now
            ):
                continue
            duration = _minutes(offer.depart_at, offer.arrive_at)
            choices.append(
                _Choice(
                    offer=offer,
                    key=offer.ref_id,
                    display_key=(offer.ref_id,),
                    mode=offer.mode,
                    band=self._item_band(employee, policy, (offer,), None),
                    score=(
                        offer.price
                        + Decimal(duration) / minutes_per_unit
                        + leg_penalty(request, leg_index, offer, profile)
                    ),
                    price=offer.price,
                    duration=duration,
                )
            )
        return choices

    def _lodging_choices(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        stay_index: int,
        hotel_choices: Sequence[HotelOffer | None],
        *,
        profile: EmployeeTravelProfileSnapshot | None = None,
    ) -> list[_Choice]:
        """第 ``stay_index`` 站住宿的候选。这一站不需要住时，唯一的候选就是"不住"。"""
        choices: list[_Choice] = []
        for hotel in hotel_choices:
            if self.validator.stay_reasons(request, stay_index, hotel):
                continue
            if hotel is None:
                choices.append(_NO_LODGING)
                continue
            choices.append(
                _Choice(
                    offer=hotel,
                    key=hotel.ref_id,
                    display_key=_hotel_display_key(hotel),
                    mode=None,
                    band=self._item_band(employee, policy, (), hotel),
                    score=hotel.total_price + lodging_penalty(request, hotel, profile),
                    price=hotel.total_price,
                    duration=0,
                )
            )
        return choices

    def _item_band(
        self,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        transports: tuple[TransportOffer, ...],
        hotel: HotelOffer | None,
    ) -> int:
        """这一项**自己**落在哪一档。整条方案的档是各项里最差的那一档。"""
        decision = self.policy_engine.evaluate(employee, policy, transports, hotel)
        return _decision_band(decision)

    def _shape_candidates(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        shape: PlanShape,
        leg_axes: list[list[_Choice]],
        stay_axes: list[list[_Choice]],
        minutes_per_unit: Decimal,
        limit: int,
        *,
        profile: EmployeeTravelProfileSnapshot | None = None,
        now: datetime,
    ) -> list[tuple[PlanShape, TravelOptionVersion]]:
        """这种走法值得摆出来的几条方案。

        此前这里是 ``product(*shaped_pools, shaped_hotels)``——把每段的报价做全组合。
        两段时是 50×50，六段时是 50⁶：按本机实测每组合约 14 µs 算要跑六十多个小时，
        不是慢一点，是做不出来。而 `_select_options` 从候选里其实只读几个极值，
        分数、价格、时长又都是**逐项相加**的，所以每个极值都等于"每一项各取最优"。
        """
        axes: list[list[_Choice]] = [
            _distinct_by_display(
                [choice for choice in axis if choice.mode is shape.modes[index]]
            )
            for index, axis in enumerate(leg_axes)
        ]
        # 每一站住宿各是一根轴——和交通段一样，逐项相加、逐项独立。
        axes.extend(_distinct_by_display(list(axis)) for axis in stay_axes)
        if any(not axis for axis in axes):
            return []

        combinations: dict[tuple[str, ...], tuple[_Choice, ...]] = {}
        for ceiling in _POLICY_CEILINGS:
            # 整条方案不超过某一档 ⇔ 每一项都不超过那一档，所以先按档收窄再各取最优。
            limited = [
                [choice for choice in axis if choice.band <= ceiling] for axis in axes
            ]
            if any(not axis for axis in limited):
                continue
            for combination in _representative_combinations(limited, limit):
                combinations.setdefault(
                    tuple(choice.key for choice in combination), combination
                )

        leg_count = len(leg_axes)
        results: list[tuple[PlanShape, TravelOptionVersion]] = []
        for combination in combinations.values():
            transports = combination[:leg_count]
            lodging = combination[leg_count:]
            option = self._evaluate(
                request,
                employee,
                policy,
                [choice.offer for choice in transports],
                [choice.offer for choice in lodging if choice.offer is not None],
                shape,
                minutes_per_unit,
                profile=profile,
                now=now,
            )
            if option is not None:
                results.append((shape, option))
        return results

    def _evaluate(
        self,
        request: TripRequestVersion,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        transports: list[TransportOffer],
        stays: Sequence[HotelOffer],
        shape: PlanShape,
        minutes_per_unit: Decimal,
        *,
        profile: EmployeeTravelProfileSnapshot | None = None,
        now: datetime,
    ) -> TravelOptionVersion | None:
        """把一种走法的一组具体报价评成一条方案；不可行则返回 None。"""
        stays = tuple(stays)
        feasibility = self.validator.validate(
            request, transports, stays, policy.arrival_buffer_minutes, now=now
        )
        if not feasibility.feasible:
            return None

        # 政策只做标注，不在这里过滤。被禁的方案也要带着理由留在结果里——
        # 否则旅行者只会看到一份莫名偏贵的列表，永远不知道最便宜那个是被政策禁的。
        # 要不要展示给用户，是展示层的决定，不是规划层的决定。
        decision = self.policy_engine.evaluate(employee, policy, transports, stays)

        total_cost = sum((item.price for item in transports), Decimal("0"))
        total_cost += sum((stay.total_price for stay in stays), Decimal("0"))
        duration = sum(_minutes(item.depart_at, item.arrive_at) for item in transports)
        penalty = preference_penalty(request, transports, stays, profile)
        # 政策**不进分数**。它是三档分类结论，不是可以被价格投票推翻的权重：
        # 折算成罚分的话，一个需审批但足够便宜的方案会排到完全合规的前面。
        # 排序改为「先按政策分档，档内再按分数」，见 _select_options。
        score = total_cost + Decimal(duration) / minutes_per_unit + penalty
        snapshot_ids = tuple(
            dict.fromkeys(
                [item.snapshot_id for item in transports]
                + [stay.snapshot_id for stay in stays]
            )
        )
        facts = [
            f"total_cost={total_cost}",
            f"currency={policy.currency}",
            f"outbound={transports[0].ref_id}",
            f"policy={decision.outcome.value}",
            f"plan_shape={shape.label()}",
            f"inventory_snapshots={','.join(snapshot_ids)}",
        ]
        # 判不了的规则要写在方案自己身上。只记一个 `policy=INSUFFICIENT_EVIDENCE`
        # 的话，看方案的人只知道"有问题"，不知道缺的是公司没填的一个数字——
        # 那正是他能拿去找管理员补的东西。和 §39 的空段说明同一条规矩。
        unjudged = decision.unjudged_rule_ids
        if unjudged:
            facts.append(f"unjudged_rules={','.join(dict.fromkeys(unjudged))}")
            facts.extend(unjudged_gap_sentences(decision))
        # 前两段沿用 outbound / inbound 这两个名字：前端、评测与冻结数据集都认它们。
        # 第三段起才用 leg2、leg3……——新名字只加在新东西上，旧的一个字不动。
        if len(transports) > 1:
            facts.append(f"inbound={transports[1].ref_id}")
        for index, leg in enumerate(transports[2:], start=2):
            facts.append(f"leg{index}={leg.ref_id}")
        # 第一处住宿沿用 hotel= / commute_minutes= 这两个名字，第二处起才是
        # stay1= / stay1_commute_minutes=——和航段那边同一条规矩。
        for index, stay in enumerate(stays):
            commute = (
                "unknown"
                if stay.commute_minutes == COMMUTE_UNKNOWN_MINUTES
                else str(stay.commute_minutes)
            )
            if index == 0:
                facts.extend([f"hotel={stay.ref_id}", f"commute_minutes={commute}"])
            else:
                facts.extend(
                    [
                        f"stay{index}={stay.ref_id}",
                        f"stay{index}_commute_minutes={commute}",
                    ]
                )
        option_key = "-".join(
            [item.ref_id for item in transports] + [stay.ref_id for stay in stays]
        )
        return TravelOptionVersion(
            option_id=f"opt-{option_key}",
            version=1,
            trip_request_version=request.version,
            inventory_snapshot_ids=snapshot_ids,
            legs=tuple(transports),
            stays=stays,
            total_cost=total_cost,
            total_duration_minutes=duration,
            feasibility=feasibility,
            policy_decision=decision,
            preference_penalty=penalty,
            score=score,
            explanation_facts=tuple(facts),
            currency=policy.currency,
        )


def _stay_pools(
    request: TripRequestVersion,
    hotel_offers: Sequence[HotelOffer] | Sequence[Sequence[HotelOffer]],
) -> list[list[HotelOffer]]:
    """按住宿站分好的酒店报价：第 i 项是第 i 站的候选。

    这趟不住店时为空列表。**只给一串酒店（不分站）时按"只有一站"理解**——
    既有调用方与冻结评测数据都是这么给的，行为和改动之前逐字一致。
    """
    stays = request.lodging_stays()
    if not stays:
        return []
    flat = bool(hotel_offers) and isinstance(hotel_offers[0], HotelOffer)
    per_stay: Sequence[Sequence[HotelOffer]] = (
        [hotel_offers] if flat else hotel_offers  # type: ignore[list-item]
    )
    return [
        list(per_stay[index]) if index < len(per_stay) else []
        for index in range(len(stays))
    ]


def _enumerate_shapes(
    pools: list[list[TransportOffer]], stay_pools: list[list[HotelOffer]]
) -> list[PlanShape]:
    """先把走法数出来：每段有哪几种交通方式的货，住宿是不是一个真选择。

    只枚举**库存里真有的**交通方式——涵盖不了的走法不该靠编造来凑。
    """
    mode_sets = [
        sorted({offer.mode for offer in pool}, key=lambda item: item.value)
        for pool in pools
    ]
    if not mode_sets or any(not modes for modes in mode_sets):
        return []
    # 住宿仍然是整趟一个"住不住"：哪几站住由请求的 `stays` 说了算，
    # 不是走法要枚举的自由度。
    lodging_choices = [True] if any(stay_pools) else [False]
    return [
        PlanShape(modes=tuple(modes), with_lodging=lodging)
        for modes in product(*mode_sets)
        for lodging in lodging_choices
    ]


#: 政策分档：合规优先，需审批其次，判不了的排在可选的最后，禁止的不可选。
#: 政策不参与分数计算。
#:
#: **"判不了"和"不许"分成两档，是这张表最要紧的一件事。** 此前它们同属一档，
#: 于是"政策表里没有成都的夜费上限"和"公司禁止商务舱"得到同样的处置——整条方案
#: 被清零（§41.3 四：机票合规、酒店也搜到了，最后 0 个方案）。缺一条公司自己没填
#: 的数字，不是旅行者违规。§35.7 原本写的就是"降到最差档"，不是"禁止"。
_POLICY_BANDS: dict[PolicyOutcome, int] = {
    PolicyOutcome.COMPLIANT: 0,
    PolicyOutcome.REQUIRES_APPROVAL: 1,
    PolicyOutcome.INSUFFICIENT_EVIDENCE: 2,
    PolicyOutcome.FORBIDDEN: 3,
}
#: 判不了：可选，但排在所有能判的后面，且必须有人批（见 `select_option`）。
_UNJUDGED_BAND = 2
#: 不可选：公司明确禁止，或方案自身的数字算不出来（混币种）。
#: 只在"它本可胜出"时作为说明留在结果里。
_BLOCKED_BAND = 3

#: 按档收窄时依次试的上限。分开试是必要的：最便宜的那条常常在更差的档里，
#: 只按"全场最便宜"取一次，合规档里最便宜的那条就再也没机会被摆出来。
_POLICY_CEILINGS: tuple[int, ...] = (0, 1, _UNJUDGED_BAND, _BLOCKED_BAND)


def _by_score(choice: _Choice) -> tuple[object, ...]:
    return (choice.score, choice.key)


def _by_price(choice: _Choice) -> tuple[object, ...]:
    return (choice.price, choice.score, choice.key)


def _by_duration(choice: _Choice) -> tuple[object, ...]:
    return (choice.duration, choice.score, choice.key)


def _representative_combinations(
    axes: list[list[_Choice]], limit: int
) -> list[tuple[_Choice, ...]]:
    """这一档里值得组装成方案的几组选择。

    `_select_options` 从候选里只读几个极值：综合最优、最便宜、最快，以及按名次
    补齐名额时每种走法的前几名。这几个量全是**逐项相加**的，可行性也逐项独立，
    所以每个极值都等于"每一项各取最优"——不必把组合枚举出来才知道谁最优。
    """
    combinations = [
        tuple(min(axis, key=objective) for axis in axes)
        for objective in (_by_score, _by_price, _by_duration)
    ]
    combinations.extend(_best_by_score(axes, limit))
    return combinations


def _best_by_score(axes: list[list[_Choice]], limit: int) -> list[tuple[_Choice, ...]]:
    """分数上的前 ``limit`` 组。

    补齐名额那一步是按名次走的，光有"最优的一组"不够。分数逐项相加，于是从最优
    那一组出发，每次只把某一项换成它的下一名，就能按序把前几组取出来——
    代价与 ``limit`` 成正比，和组合总数无关。
    """
    ordered = [sorted(axis, key=_by_score) for axis in axes]
    start = (0,) * len(ordered)
    heap: list[tuple[Decimal, tuple[int, ...]]] = [(_axes_score(ordered, start), start)]
    seen = {start}
    picked: list[tuple[_Choice, ...]] = []
    while heap and len(picked) < limit:
        _, indexes = heappop(heap)
        picked.append(tuple(ordered[axis][index] for axis, index in enumerate(indexes)))
        for axis, index in enumerate(indexes):
            if index + 1 >= len(ordered[axis]):
                continue
            successor = indexes[:axis] + (index + 1,) + indexes[axis + 1 :]
            if successor in seen:
                continue
            seen.add(successor)
            heappush(heap, (_axes_score(ordered, successor), successor))
    return picked


def _axes_score(ordered: list[list[_Choice]], indexes: tuple[int, ...]) -> Decimal:
    return sum(
        (ordered[axis][index].score for axis, index in enumerate(indexes)),
        Decimal("0"),
    )


def _policy_band(option: TravelOptionVersion) -> int:
    return _decision_band(option.policy_decision)


def _decision_band(decision: PolicyDecision) -> int:
    """这条判定落在哪一档。

    **先看有没有"禁止"，再看聚合结论。** 政策引擎的严重度是"证据不足 > 禁止"
    （缺数据比违规更该先说出口），所以一条既违规又缺数据的方案聚合出来是
    `INSUFFICIENT_EVIDENCE`。证据不足这一档现在可选，只认聚合结论的话，
    被禁的方案会从"判不了"这道门溜出去。

    "判不了"要能摆出来让人批，得先有可批的材料；没有的（总价算不出来、
    一条规则都没真正判过）也归到不可选，理由见 `unreviewable_reasons`。
    """
    if decision.forbidden_rule_ids or unreviewable_reasons(decision):
        return _BLOCKED_BAND
    return _POLICY_BANDS.get(decision.outcome, _BLOCKED_BAND)


def _rank_key(entry: tuple[PlanShape, TravelOptionVersion]) -> tuple[object, ...]:
    option = entry[1]
    return (_policy_band(option), option.score, option.option_id)


def _select_options(
    candidates: list[tuple[PlanShape, TravelOptionVersion]],
    limit: int,
    *,
    compare_modes: bool = False,
) -> list[TravelOptionVersion]:
    """选出要摆给旅行者的几条：先分档，档内按类别取，再按走法补齐。

    **类别**指的是"它是哪一类里最好的"——最便宜的、最快的、综合最合适的。
    差旅没有唯一的最好，把这些揉成一个分数再取前三，等于替旅行者做了他该做的取舍。

    ``compare_modes`` 对应偏好 `compare_train_and_flight`："我想比比高铁和飞机"。
    这句话要的不是罚分，而是**摆出来的这几个里两种都得有**。
    """
    ranked = sorted(candidates, key=_rank_key)
    eligible: list[tuple[PlanShape, TravelOptionVersion]] = []
    blocked: list[TravelOptionVersion] = []
    seen: set[tuple[object, ...]] = set()
    for shape, option in ranked:
        fingerprint = _display_fingerprint(option)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if _policy_band(option) < _BLOCKED_BAND:
            eligible.append((shape, option))
        else:
            blocked.append(option)

    if not eligible:
        return []

    chosen = _choose_by_category(eligible, limit)
    if compare_modes:
        chosen = _cover_every_available_mode(chosen, eligible, limit)

    categories = _category_labels(eligible, chosen)
    selectable = [
        replace(
            option,
            explanation_facts=(
                *option.explanation_facts,
                f"category={categories[option.option_id]}",
            ),
        )
        for _, option in sorted(chosen, key=_rank_key)
    ]
    best_selectable = min(item.score for item in selectable)
    would_have_won = [item for item in blocked if item.score < best_selectable]
    # 被政策挡住的方案只在**本可胜出**时才保留：那正是"最便宜的那个被政策禁了"
    # 这句话需要说出口的情形。比可选方案还差的被挡方案只是噪音，不保留。
    return selectable + would_have_won[:1]


def _choose_by_category(
    eligible: list[tuple[PlanShape, TravelOptionVersion]], limit: int
) -> list[tuple[PlanShape, TravelOptionVersion]]:
    """每一类各取一个代表，再按"还没出现过的走法"优先补齐到 ``limit``。"""
    chosen: list[tuple[PlanShape, TravelOptionVersion]] = []

    def take(entry: tuple[PlanShape, TravelOptionVersion] | None) -> None:
        if entry is None or len(chosen) >= limit:
            return
        if any(item[1].option_id == entry[1].option_id for item in chosen):
            return
        chosen.append(entry)

    take(eligible[0])  # 综合最合适的：排名本身就是这一类。
    take(min(eligible, key=lambda item: (item[1].total_cost, _rank_key(item))))
    take(min(eligible, key=lambda item: (item[1].total_duration_minutes, _rank_key(item))))

    # 补齐时优先换一个还没出现过的走法，别让同一个走法的价格变体占满名单。
    for prefer_new_shape in (True, False):
        for entry in eligible:
            if len(chosen) >= limit:
                break
            if prefer_new_shape and any(item[0] == entry[0] for item in chosen):
                continue
            take(entry)
    return sorted(chosen, key=_rank_key)


def _category_labels(
    eligible: list[tuple[PlanShape, TravelOptionVersion]],
    chosen: list[tuple[PlanShape, TravelOptionVersion]],
) -> dict[str, str]:
    """给每条入选方案贴一句"它凭什么在这儿"，好让解释说得出彼此差在哪。"""
    cheapest = min(eligible, key=lambda item: (item[1].total_cost, _rank_key(item)))[1]
    fastest = min(
        eligible, key=lambda item: (item[1].total_duration_minutes, _rank_key(item))
    )[1]
    best_overall = eligible[0][1]
    labels: dict[str, str] = {}
    for _, option in chosen:
        tags = []
        if option.option_id == best_overall.option_id:
            tags.append("best_overall")
        if option.option_id == cheapest.option_id:
            tags.append("cheapest")
        if option.option_id == fastest.option_id:
            tags.append("fastest")
        labels[option.option_id] = "|".join(tags) if tags else "alternative"
    return labels


def _modes_used(option: TravelOptionVersion) -> frozenset[TransportMode]:
    return frozenset(leg.mode for leg in option.legs)


def _cover_every_available_mode(
    chosen: list[tuple[PlanShape, TravelOptionVersion]],
    eligible: list[tuple[PlanShape, TravelOptionVersion]],
    limit: int,
) -> list[tuple[PlanShape, TravelOptionVersion]]:
    """让入选名单涵盖库存里真实存在的每种交通方式，名额不变。

    库存里只有飞机时什么都不做——涵盖不了的东西不该靠编造来凑。
    """
    available = {
        mode
        for _, option in eligible
        for mode in _modes_used(option)
        if mode in {TransportMode.TRAIN, TransportMode.FLIGHT}
    }
    result = sorted(chosen, key=_rank_key)
    for mode in sorted(available, key=lambda item: item.value):
        if any(mode in _modes_used(option) for _, option in result):
            continue
        replacement = next(
            (
                entry
                for entry in eligible
                if mode in _modes_used(entry[1])
                and all(entry[1].option_id != item[1].option_id for item in result)
            ),
            None,
        )
        if replacement is None:
            continue
        if len(result) < limit:
            result.append(replacement)
        else:
            result[-1] = replacement
        result = sorted(result, key=_rank_key)
    return result


def _hotel_display_key(hotel: HotelOffer | None) -> tuple[object, ...]:
    """住宿摆出来长什么样。两家酒店这一项相同，结果里就是同一条方案。"""
    if hotel is None:
        return ()
    return (
        hotel.name.casefold(),
        hotel.city.casefold(),
        hotel.nightly_price,
        hotel.nights,
        hotel.check_in,
        hotel.check_out,
    )


def _display_fingerprint(option: TravelOptionVersion) -> tuple[object, ...]:
    return (
        tuple(leg.ref_id for leg in option.legs),
        tuple(_hotel_display_key(stay) for stay in option.stays),
    )


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)
