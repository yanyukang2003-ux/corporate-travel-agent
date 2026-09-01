"""上下文数据层：从历史行程看出习惯，以及这份习惯**只能改到哪里为止**。

三组：

- `DeriveTests` —— 推断本身：几趟才算习惯、多大比例才算、证据说不说得出口。
- `WeightTests` —— 推断和这一轮说的话怎么共处：说过的永远压过推断的。
- `ProfileStaysOutOfTheVerdictTests` —— **最重要的一组**。画像不参与可行性、
  不参与政策判定、不改变候选池。这一层一旦能碰这三样，它就变成了绕过合规的后门。
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    PolicyOutcome,
    PreferenceOrigin,
    TaskState,
    TransportMode,
)
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    EmployeeTravelProfileSnapshot,
    FeasibilityResult,
    HotelOffer,
    PolicyDecision,
    ProfilePreference,
    TransportOffer,
    TravelOptionVersion,
    TripTask,
)
from corporate_travel_agent.planning.preferences import (
    leg_penalty,
    lodging_penalty,
    weighted_profile_preferences,
)
from corporate_travel_agent.services.travel_profile import (
    RepositoryTripHistory,
    derive_travel_profile,
)

_BASE = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

_EMPLOYEE = EmployeeProfileSnapshot(
    snapshot_id="emp-1",
    employee_id="E1001",
    level="L3",
    department="Sales",
    home_city="Beijing",
    manager_id="M2001",
)


def _leg(mode: TransportMode, *, hour: int = 9, day: int = 1) -> TransportOffer:
    depart = _BASE.replace(day=day, hour=hour)
    return TransportOffer(
        ref_id=f"{mode.value}-{day}-{hour}",
        snapshot_id="snap",
        provider="mock",
        mode=mode,
        origin="Beijing",
        destination="Shanghai",
        depart_at=depart,
        arrive_at=depart + timedelta(hours=2),
        price=Decimal("500"),
        seat_class="ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS",
    )


def _stay(name: str, commute_minutes: int) -> HotelOffer:
    return HotelOffer(
        ref_id=f"hotel-{name}-{commute_minutes}",
        snapshot_id="snap",
        provider="mock",
        name=name,
        city="Shanghai",
        check_in=_BASE.date(),
        check_out=(_BASE + timedelta(days=1)).date(),
        nightly_price=Decimal("400"),
        commute_minutes=commute_minutes,
    )


def _completed_task(
    task_id: str,
    *,
    legs: tuple[TransportOffer, ...],
    stays: tuple[HotelOffer, ...] = (),
    employee: EmployeeProfileSnapshot = _EMPLOYEE,
    state: TaskState = TaskState.HANDED_OFF,
    select: bool = True,
) -> TripTask:
    option = TravelOptionVersion(
        option_id=f"opt-{task_id}",
        version=1,
        trip_request_version=1,
        inventory_snapshot_ids=("snap",),
        legs=legs,
        stays=stays,
        total_cost=Decimal("1000"),
        total_duration_minutes=120,
        feasibility=FeasibilityResult(feasible=True, reasons=()),
        policy_decision=PolicyDecision(outcome=PolicyOutcome.COMPLIANT, evidence=()),
        preference_penalty=Decimal("0"),
        score=Decimal("0"),
        explanation_facts=(),
    )
    return TripTask(
        task_id=task_id,
        state=state,
        request=None,
        employee=employee,
        policy_snapshot_id="policy-1",
        options=[option],
        selected_option_id=option.option_id if select else None,
    )


class _StubHistory:
    def __init__(self, own=(), peers=()) -> None:
        self._own = tuple(own)
        self._peers = tuple(peers)

    def completed_trips(self, employee_id, *, limit=50):
        return self._own[:limit]

    def peer_completed_trips(self, *, level, home_city, limit=200):
        return self._peers[:limit]


class DeriveTests(unittest.TestCase):
    def test_two_trips_are_not_a_habit(self) -> None:
        """一次是偶然、两次是巧合。从两趟就推出习惯并一直照着排，最容易失去信任。"""
        history = _StubHistory(
            own=[
                _completed_task("t1", legs=(_leg(TransportMode.TRAIN),)),
                _completed_task("t2", legs=(_leg(TransportMode.TRAIN, day=2),)),
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        self.assertEqual(profile.preferences, ())
        self.assertEqual(profile.derived_from_trips, 2)

    def test_three_consistent_trips_become_a_habit_with_a_reason(self) -> None:
        history = _StubHistory(
            own=[
                _completed_task(f"t{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
                for i in range(1, 4)
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        train = next(
            item for item in profile.preferences if item.name == "prefer_train"
        )
        self.assertIs(train.origin, PreferenceOrigin.OBSERVED)
        # 证据必须是一句能念给员工听的话，而且带得出数字。
        self.assertIn("高铁", train.evidence)
        self.assertIn("3", train.evidence)

    def test_a_split_record_is_not_a_habit(self) -> None:
        """五段里三段高铁说明不了什么，那叫随机。"""
        history = _StubHistory(
            own=[
                _completed_task(
                    "t1",
                    legs=(_leg(TransportMode.TRAIN), _leg(TransportMode.FLIGHT, hour=11)),
                ),
                _completed_task(
                    "t2",
                    legs=(
                        _leg(TransportMode.TRAIN, day=2),
                        _leg(TransportMode.FLIGHT, day=2, hour=11),
                    ),
                ),
                _completed_task("t3", legs=(_leg(TransportMode.TRAIN, day=3),)),
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        self.assertEqual(
            [item.name for item in profile.preferences if "prefer_" in item.name], []
        )

    def test_only_trips_the_traveller_actually_booked_count(self) -> None:
        """看过不算数，去订了才算表态。"""
        history = _StubHistory(
            own=[
                _completed_task("t1", legs=(_leg(TransportMode.TRAIN),)),
                _completed_task("t2", legs=(_leg(TransportMode.TRAIN, day=2),)),
                # 状态没到交接，不算。
                _completed_task(
                    "t3", legs=(_leg(TransportMode.TRAIN, day=3),), state=TaskState.WAITING_FOR_USER
                ),
                # 到了交接但没选方案，那趟出行的选择根本没发生。
                _completed_task("t4", legs=(_leg(TransportMode.TRAIN, day=4),), select=False),
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        # 真正表过态的只有两趟，达不到三趟的门槛。
        self.assertEqual(profile.preferences, ())

    def test_unknown_commute_is_not_counted_as_far(self) -> None:
        """`COMMUTE_UNKNOWN_MINUTES` 是"供应商证明不了"，不是"很远"。

        当成远的算，会从缺数据里推出一个假习惯。
        """
        history = _StubHistory(
            own=[
                _completed_task(
                    f"t{i}",
                    legs=(_leg(TransportMode.TRAIN, day=i),),
                    stays=(_stay("Unknown Hotel", COMMUTE_UNKNOWN_MINUTES),),
                )
                for i in range(1, 5)
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        self.assertNotIn(
            "hotel_near_client", [item.name for item in profile.preferences]
        )

    def test_staying_near_the_client_becomes_a_habit(self) -> None:
        history = _StubHistory(
            own=[
                _completed_task(
                    f"t{i}",
                    legs=(_leg(TransportMode.TRAIN, day=i),),
                    stays=(_stay("Near Hotel", 10),),
                )
                for i in range(1, 5)
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        near = next(
            item for item in profile.preferences if item.name == "hotel_near_client"
        )
        self.assertIs(near.origin, PreferenceOrigin.OBSERVED)

    def test_a_hotel_chosen_twice_is_remembered(self) -> None:
        """第二次是他自己又选了一遍，才算数。"""
        history = _StubHistory(
            own=[
                _completed_task(
                    "t1",
                    legs=(_leg(TransportMode.TRAIN),),
                    stays=(_stay("Repeat Inn", 10),),
                ),
                _completed_task(
                    "t2",
                    legs=(_leg(TransportMode.TRAIN, day=2),),
                    stays=(_stay("Repeat Inn", 10),),
                ),
                _completed_task(
                    "t3",
                    legs=(_leg(TransportMode.TRAIN, day=3),),
                    stays=(_stay("One Off", 10),),
                ),
            ]
        )

        profile = derive_travel_profile(_EMPLOYEE, history)

        self.assertEqual(profile.preferred_hotels, ("Repeat Inn",))

    def test_cold_start_falls_back_to_same_level_same_city_colleagues(self) -> None:
        """新员工一趟历史都没有——这时组织默认值是唯一能说的话。"""
        peers = [
            _completed_task(f"p{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
            for i in range(1, 5)
        ]
        history = _StubHistory(own=[], peers=peers)

        profile = derive_travel_profile(_EMPLOYEE, history)

        train = next(
            item for item in profile.preferences if item.name == "prefer_train"
        )
        # 它根本不是关于他本人的，所以来源必须标成组织默认值而不是"观察到的"。
        self.assertIs(train.origin, PreferenceOrigin.ORG_DEFAULT)
        self.assertIn("L3", train.evidence)
        self.assertEqual(profile.derived_from_trips, 0)

    def test_own_history_wins_over_colleagues(self) -> None:
        """他本人的选择比同级平均值更能代表他，够了就不再掺同事。"""
        own = [
            _completed_task(f"t{i}", legs=(_leg(TransportMode.FLIGHT, day=i),))
            for i in range(1, 5)
        ]
        peers = [
            _completed_task(f"p{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
            for i in range(1, 5)
        ]
        history = _StubHistory(own=own, peers=peers)

        profile = derive_travel_profile(_EMPLOYEE, history)

        names = {item.name for item in profile.preferences}
        self.assertIn("prefer_flight", names)
        self.assertNotIn("prefer_train", names)


    def test_the_snapshot_id_changes_when_the_content_changes(self) -> None:
        """同一个 ID 不能对应两份内容。

        员工档案版本不变、但他又订了两趟行程时画像会变。只按 profile_version 命名的话，
        两份不同的画像共用一个 ID，"这趟当初按哪一份排的"就答不上来了。
        """
        three = _StubHistory(
            own=[
                _completed_task(f"t{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
                for i in range(1, 4)
            ]
        )
        four = _StubHistory(
            own=[
                _completed_task(f"t{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
                for i in range(1, 5)
            ]
        )

        first = derive_travel_profile(_EMPLOYEE, three)
        second = derive_travel_profile(_EMPLOYEE, four)

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        # 同一份历史算两次必须得到同一个 ID，否则"钉住"就没有意义。
        self.assertEqual(
            first.snapshot_id, derive_travel_profile(_EMPLOYEE, three).snapshot_id
        )


class WeightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = make_demo_request(task_id="weights")

    def _profile(self, *preferences: ProfilePreference) -> EmployeeTravelProfileSnapshot:
        return EmployeeTravelProfileSnapshot(
            snapshot_id="p1", employee_id="E1001", preferences=preferences
        )

    def test_this_turn_silences_the_whole_conflicting_family(self) -> None:
        """他这次说要飞，「平时坐高铁」就不该再往下压分。

        整族让位而不是只让同名的那一个：留着 prefer_train 会和 prefer_flight
        互相抵消，排出来的顺序谁也解释不了。
        """
        profile = self._profile(
            ProfilePreference("prefer_train", PreferenceOrigin.OBSERVED, "历史里多是高铁")
        )

        weights = weighted_profile_preferences(frozenset({"prefer_flight"}), profile)

        self.assertEqual(weights, {})

    def test_an_unrelated_habit_still_speaks(self) -> None:
        """让位是按族让，不是把整份画像作废。"""
        profile = self._profile(
            ProfilePreference("prefer_train", PreferenceOrigin.OBSERVED, "a"),
            ProfilePreference("avoid_early_departure", PreferenceOrigin.OBSERVED, "b"),
        )

        weights = weighted_profile_preferences(frozenset({"prefer_flight"}), profile)

        self.assertEqual(list(weights), ["avoid_early_departure"])

    def test_a_stated_preference_is_not_double_counted(self) -> None:
        profile = self._profile(
            ProfilePreference("prefer_train", PreferenceOrigin.OBSERVED, "a")
        )

        weights = weighted_profile_preferences(frozenset({"prefer_train"}), profile)

        self.assertEqual(weights, {})

    def test_weaker_evidence_pushes_less_hard(self) -> None:
        """他自己填的 > 从历史推的 > 同事的常见选择。"""
        early_flight = _leg(TransportMode.FLIGHT, hour=5)
        request = make_demo_request(task_id="strength")
        # 演示请求本身声明了 avoid_early_departure，会盖过画像，所以这里换一个
        # 没有声明它的请求来单独看画像的分量。
        from dataclasses import replace

        bare = replace(request, soft_preferences=(), scoped_soft_preferences=())

        penalties = [
            leg_penalty(
                bare,
                0,
                early_flight,
                self._profile(
                    ProfilePreference("avoid_early_departure", origin, "e")
                ),
            )
            for origin in (
                PreferenceOrigin.DECLARED,
                PreferenceOrigin.OBSERVED,
                PreferenceOrigin.ORG_DEFAULT,
            )
        ]

        self.assertGreater(penalties[0], penalties[1])
        self.assertGreater(penalties[1], penalties[2])
        self.assertGreater(penalties[2], 0)
        # 亲口说的仍然最重。
        stated = leg_penalty(request, 0, early_flight)
        self.assertGreater(stated, penalties[0])

    def test_the_same_habit_from_two_sources_is_not_added_up(self) -> None:
        """同一件事被推断了两次，不等于更确定。"""
        profile = self._profile(
            ProfilePreference("prefer_train", PreferenceOrigin.OBSERVED, "a"),
            ProfilePreference("prefer_train", PreferenceOrigin.ORG_DEFAULT, "b"),
        )

        weights = weighted_profile_preferences(frozenset(), profile)

        self.assertEqual(list(weights), ["prefer_train"])
        self.assertEqual(weights["prefer_train"], Decimal("0.4"))

    def test_a_hotel_stayed_at_twice_scores_better(self) -> None:
        profile = EmployeeTravelProfileSnapshot(
            snapshot_id="p1",
            employee_id="E1001",
            preferred_hotels=("Repeat Inn",),
        )
        repeat = _stay("Repeat Inn", 10)
        other = _stay("Other Inn", 10)

        self.assertLess(
            lodging_penalty(self.request, repeat, profile),
            lodging_penalty(self.request, other, profile),
        )

    def test_no_profile_behaves_exactly_as_before(self) -> None:
        """默认不接画像时，罚分要和加这一层之前逐字一样。"""
        offer = _leg(TransportMode.FLIGHT, hour=5)

        self.assertEqual(
            leg_penalty(self.request, 0, offer, None),
            leg_penalty(self.request, 0, offer),
        )


class ProfileStaysOutOfTheVerdictTests(unittest.TestCase):
    """画像只改排序。它一旦能碰可行性或政策，就成了绕过合规的后门。"""

    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        self.request = make_demo_request(task_id="invariant")
        self.employee = self.workflow.employees.snapshot("E1001")
        self.policy = self.workflow.policies.current()
        self.profile = EmployeeTravelProfileSnapshot(
            snapshot_id="p1",
            employee_id="E1001",
            preferences=(
                ProfilePreference("prefer_flight", PreferenceOrigin.OBSERVED, "e"),
                ProfilePreference("avoid_early_departure", PreferenceOrigin.DECLARED, "e"),
            ),
            preferred_hotels=("Near Client Hotel",),
        )

    def _plan(self, profile):
        task = self.workflow.create_task(
            make_demo_request(task_id=f"inv-{'with' if profile else 'without'}")
        )
        snapshots = self.workflow.tasks.snapshots(task.task_id)
        offers = [item for snapshot in snapshots for item in snapshot.items]
        transports = [item for item in offers if isinstance(item, TransportOffer)]
        hotels = [item for item in offers if isinstance(item, HotelOffer)]
        return self.workflow.planner.plan(
            request=self.request,
            employee=self.employee,
            policy=self.policy,
            leg_offers=[transports, transports],
            hotel_offers=[hotels],
            profile=profile,
            limit=50,
            now=DEMO_CLOCK,
        )

    def test_the_candidate_pool_is_identical(self) -> None:
        """同一批库存，带不带画像，够格摆出来的方案必须是同一批。"""
        without = {item.option_id for item in self._plan(None)}
        with_profile = {item.option_id for item in self._plan(self.profile)}

        self.assertTrue(without)
        self.assertEqual(without, with_profile)

    def test_every_policy_verdict_is_identical(self) -> None:
        """同一条方案的政策结论和每一条证据，一个字都不能因为画像而变。"""
        without = {item.option_id: item for item in self._plan(None)}
        with_profile = {item.option_id: item for item in self._plan(self.profile)}

        for option_id, plain in without.items():
            with self.subTest(option=option_id):
                shaped = with_profile[option_id]
                self.assertEqual(
                    plain.policy_decision.outcome, shaped.policy_decision.outcome
                )
                self.assertEqual(
                    plain.policy_decision.evidence, shaped.policy_decision.evidence
                )

    def test_feasibility_is_identical(self) -> None:
        without = {item.option_id: item.feasibility for item in self._plan(None)}
        with_profile = {
            item.option_id: item.feasibility for item in self._plan(self.profile)
        }

        self.assertEqual(without, with_profile)

    def test_the_profile_does_change_the_ranking(self) -> None:
        """反面：如果它什么都没改，那这一层就是白加的。"""
        without = self._plan(None)
        with_profile = self._plan(self.profile)

        plain = {item.option_id: item.preference_penalty for item in without}
        shaped = {item.option_id: item.preference_penalty for item in with_profile}

        self.assertNotEqual(plain, shaped)


class OrchestratorWiringTests(unittest.TestCase):
    """接上和不接上，分别该是什么样。"""

    def _history_with_train_habit(self) -> _StubHistory:
        return _StubHistory(
            own=[
                _completed_task(f"h{i}", legs=(_leg(TransportMode.TRAIN, day=i),))
                for i in range(1, 5)
            ]
        )

    def test_the_layer_is_off_by_default(self) -> None:
        """默认不接历史来源，行为和加这一层之前逐字一样。

        这不是没做完：接上画像那一刻排序分数就变了，既有评测基线和新数字
        就不能放在同一张图上比。
        """
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

        task = workflow.create_task(make_demo_request(task_id="off"))

        self.assertIsNone(workflow.trip_history)
        self.assertNotIn("travel_profile", task.metadata)

    def test_the_profile_is_pinned_to_the_task(self) -> None:
        """算出来就钉住。员工下个月习惯变了，这趟"当初为什么这么排"仍然答得上来。"""
        workflow, _ = build_demo_system(
            clock=lambda: DEMO_CLOCK, trip_history=self._history_with_train_habit()
        )

        task = workflow.create_task(make_demo_request(task_id="pinned"))

        pinned = task.metadata["travel_profile"]
        self.assertEqual(
            [item["name"] for item in pinned["preferences"]],
            ["avoid_early_departure", "prefer_train"],
        )
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("TRAVEL_PROFILE_PINNED", events)

    def test_a_pinned_profile_survives_a_json_round_trip(self) -> None:
        """从数据库恢复时 origin 是字符串。不还原成枚举，画像会**静默失效**——
        不报错，只是排序悄悄变回没有画像的样子。"""
        from corporate_travel_agent.agent.orchestrator import (
            _travel_profile_from_metadata,
        )

        workflow, _ = build_demo_system(
            clock=lambda: DEMO_CLOCK, trip_history=self._history_with_train_habit()
        )
        task = workflow.create_task(make_demo_request(task_id="round-trip"))
        payload = json.loads(json.dumps(task.metadata["travel_profile"], default=str))

        restored = _travel_profile_from_metadata(payload)

        self.assertTrue(restored.preferences)
        for item in restored.preferences:
            with self.subTest(preference=item.name):
                self.assertIsInstance(item.origin, PreferenceOrigin)
        # 还原出来的画像在权重表里查得到，不是一个查不到的字符串。
        weights = weighted_profile_preferences(frozenset(), restored)
        self.assertTrue(weights)

    def test_a_broken_history_source_never_fails_the_task(self) -> None:
        """画像只是排序上的加成。读历史出问题就当没有它，照常按这一轮说的话排。"""

        class _Broken:
            def completed_trips(self, employee_id, *, limit=50):
                raise RuntimeError("history store is down")

            def peer_completed_trips(self, *, level, home_city, limit=200):
                raise RuntimeError("history store is down")

        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK, trip_history=_Broken())

        task = workflow.create_task(make_demo_request(task_id="broken"))

        self.assertTrue(task.options)
        self.assertNotIn("travel_profile", task.metadata)
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("TRAVEL_PROFILE_UNAVAILABLE", events)

    def test_repository_history_only_returns_booked_trips(self) -> None:
        """适配器这一侧也要过滤，别把没订成的行程当作表态。"""
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        workflow.tasks.add(_completed_task("done", legs=(_leg(TransportMode.TRAIN),)))
        workflow.tasks.add(
            _completed_task(
                "abandoned",
                legs=(_leg(TransportMode.TRAIN, day=2),),
                state=TaskState.WAITING_FOR_USER,
            )
        )

        history = RepositoryTripHistory(workflow.tasks)

        booked = [item.task_id for item in history.completed_trips("E1001")]
        self.assertEqual(booked, ["done"])


if __name__ == "__main__":
    unittest.main()
