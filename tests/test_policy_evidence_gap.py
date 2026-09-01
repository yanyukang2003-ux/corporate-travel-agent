"""「政策判不了」和「公司不许」是两件事，这里盯住它们不再被当成同一件。

背景是 §41.3（四）抓到的真缺陷：北京→成都机票搜到了也合规、酒店也搜到 9 家，
只因为政策表里没有成都的夜费上限，整趟行程 **0 个方案**。缺一条公司自己没填的
数字，不该等于旅行者违规。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicyDecision,
    PolicySnapshot,
    RuleEvidence,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.planning.planner import ItineraryPlanner, _decision_band
from corporate_travel_agent.policy.engine import (
    EVIDENCE_INVALIDATING_RULE_IDS,
    NO_JUDGED_RULE,
    unreviewable_reasons,
)
from corporate_travel_agent.policy.gaps import _SENTENCES, unjudged_gap_sentences

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def _policy(**overrides: object) -> PolicySnapshot:
    payload: dict[str, object] = {
        "snapshot_id": "gap-policy-v1",
        "policy_version": "gap-v1",
        "level_rules": {"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        # 上海**故意没有**夜费上限：这就是成都那一类城市的形状。
        "hotel_city_caps": {},
        "arrival_buffer_minutes": 30,
        "exception_allowed_rule_ids": frozenset(),
        "effective_from": date(2026, 1, 1),
        "currency": "CNY",
    }
    payload.update(overrides)
    return PolicySnapshot(**payload)  # type: ignore[arg-type]


def _employee(level: str = "L1") -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="gap-employee-v1",
        employee_id="E-GAP",
        level=level,
        department="Product",
        home_city="Beijing",
        manager_id="M-GAP",
    )


def _request() -> TripRequestVersion:
    return TripRequestVersion(
        task_id="gap-task",
        version=1,
        traveler_id="E-GAP",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 18, 0, tzinfo=SH),
        return_after=None,
        return_before=None,
        hotel_check_in=date(2026, 8, 20),
        hotel_check_out=date(2026, 8, 21),
        hard_constraints=("hotel_required",),
        soft_preferences=(),
    )


def _flight(ref_id: str = "OUT-1", *, seat_class: str = "ECONOMY") -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-flight",
        provider="duffel",
        mode=TransportMode.FLIGHT,
        origin="Beijing",
        destination="Shanghai",
        depart_at=datetime(2026, 8, 20, 14, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 20, 16, 0, tzinfo=SH),
        price=Decimal("35"),
        seat_class=seat_class,
        currency="CNY",
    )


def _hotel(ref_id: str = "HT-1") -> HotelOffer:
    return HotelOffer(
        ref_id=ref_id,
        snapshot_id="snap-hotel",
        provider="liteapi",
        name="Cambria Hotel",
        city="Shanghai",
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
        nightly_price=Decimal("120.80"),
        commute_minutes=20,
        currency="CNY",
    )


def _decision(*outcomes: PolicyOutcome) -> PolicyDecision:
    evidence = tuple(
        RuleEvidence(
            rule_id=f"rule.{index}",
            actual="x",
            threshold="y",
            policy_version="gap-v1",
            outcome=outcome,
            message="",
            exception_allowed=False,
        )
        for index, outcome in enumerate(outcomes)
    )
    return PolicyDecision(outcomes[0], evidence)


class TestPlannerBands:
    def test_a_city_without_a_nightly_cap_still_gets_options(self) -> None:
        """§41.3（四）的回归：缺一条公司没填的上限，不该把整趟行程清零。"""
        options = ItineraryPlanner().plan(
            _request(),
            _employee(),
            _policy(),
            leg_offers=[[_flight()]],
            hotel_offers=[_hotel()],
            now=NOW,
        )

        assert options, "缺夜费上限不该等于没有方案"
        assert options[0].policy_decision.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE

    def test_the_option_says_which_rule_it_could_not_judge(self) -> None:
        """光说"有问题"没用；要说清缺的是哪一条，那才是能拿去补的东西。"""
        options = ItineraryPlanner().plan(
            _request(),
            _employee(),
            _policy(),
            leg_offers=[[_flight()]],
            hotel_offers=[_hotel()],
            now=NOW,
        )
        facts = options[0].explanation_facts

        assert "unjudged_rules=hotel.city.nightly_cap" in facts
        # 说给旅行者的那句话是中文的，不是 `hotel.city.nightly_cap →
        # INSUFFICIENT_EVIDENCE` 这种给运维看的内部串（§41.3 五）。
        sentence = next(item for item in facts if item.startswith("公司政策里还没有"))
        assert "Shanghai" in sentence
        assert "夜费上限" in sentence

    def test_judged_options_rank_ahead_of_unjudged_ones(self) -> None:
        """"降到最差档"是排最后，不是踢出去；被禁的仍然在判不了的后面。

        判不了的那一档要配一条判过的规则才成立（否则没有可批的材料，
        见 `unreviewable_reasons`），所以这里给它搭一条合规证据。
        """
        bands = [
            _decision_band(_decision(PolicyOutcome.COMPLIANT)),
            _decision_band(_decision(PolicyOutcome.REQUIRES_APPROVAL)),
            _decision_band(
                _decision(PolicyOutcome.INSUFFICIENT_EVIDENCE, PolicyOutcome.COMPLIANT)
            ),
            _decision_band(_decision(PolicyOutcome.FORBIDDEN)),
        ]

        assert bands == sorted(bands)
        assert len(set(bands)) == 4, "四种结论要落在四个不同的档上"

    def test_a_forbidden_rule_is_not_laundered_by_a_missing_one(self) -> None:
        """既违规又缺数据的方案聚合成"证据不足"——它不能从判不了那道门溜出去。

        政策引擎的严重度是"证据不足 > 禁止"，所以这种方案的 `outcome` 和一条
        纯粹缺数据的方案**一模一样**。只认 outcome 就会把被禁的放出去。
        """
        laundered = replace(
            _decision(PolicyOutcome.FORBIDDEN, PolicyOutcome.INSUFFICIENT_EVIDENCE),
            outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
        )
        merely_unjudged = _decision(
            PolicyOutcome.INSUFFICIENT_EVIDENCE, PolicyOutcome.COMPLIANT
        )

        assert laundered.outcome is merely_unjudged.outcome
        assert _decision_band(laundered) == _decision_band(
            _decision(PolicyOutcome.FORBIDDEN)
        )
        assert _decision_band(laundered) > _decision_band(merely_unjudged)

    def test_business_class_stays_blocked_when_the_hotel_cap_is_missing(self) -> None:
        """端到端跑一遍上面那条：舱位超标 + 缺上限 = 一个方案都不给。"""
        options = ItineraryPlanner().plan(
            _request(),
            _employee(),
            _policy(),
            leg_offers=[[_flight(seat_class="BUSINESS")]],
            hotel_offers=[_hotel()],
            now=NOW,
        )

        assert options == []

    def test_an_unknown_level_still_yields_nothing(self) -> None:
        """职级不在政策表里时一条规则都没判过，没有可批的材料——维持原样。"""
        options = ItineraryPlanner().plan(
            _request(),
            _employee(level="L-UNKNOWN"),
            _policy(),
            leg_offers=[[_flight()]],
            hotel_offers=[_hotel()],
            now=NOW,
        )

        assert options == []


class TestUnreviewableReasons:
    def test_a_decision_with_no_judged_rule_is_unreviewable(self) -> None:
        decision = _decision(PolicyOutcome.INSUFFICIENT_EVIDENCE)

        assert unreviewable_reasons(decision) == (NO_JUDGED_RULE,)

    def test_a_named_gap_beside_a_judged_rule_is_reviewable(self) -> None:
        decision = _decision(
            PolicyOutcome.INSUFFICIENT_EVIDENCE, PolicyOutcome.COMPLIANT
        )

        assert unreviewable_reasons(decision) == ()

    @pytest.mark.parametrize("rule_id", sorted(EVIDENCE_INVALIDATING_RULE_IDS))
    def test_some_gaps_invalidate_the_rest_of_the_evidence(self, rule_id: str) -> None:
        """总价算不出来、政策这几天不生效——这两条一破，"判过了"就是假材料。

        `boundary-policy-expired-038` 就死在这上面：政策失效窗口之外，
        舱位和夜费仍然被判成合规，但那是拿一份不适用的政策判的。
        """
        decision = PolicyDecision(
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
            (
                RuleEvidence(
                    rule_id=rule_id,
                    actual="x",
                    threshold="y",
                    policy_version="gap-v1",
                    outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
                    message="",
                    exception_allowed=False,
                ),
                RuleEvidence(
                    rule_id="transport.flight.seat_class",
                    actual="ECONOMY",
                    threshold="ECONOMY",
                    policy_version="gap-v1",
                    outcome=PolicyOutcome.COMPLIANT,
                    message="",
                    exception_allowed=False,
                ),
            ),
        )

        assert unreviewable_reasons(decision) == (rule_id,)
        assert _decision_band(decision) == _decision_band(
            _decision(PolicyOutcome.FORBIDDEN)
        )


class TestGapSentences:
    def test_every_rule_that_can_be_unjudged_has_a_sentence(self) -> None:
        """政策引擎能产出"证据不足"的规则，每一条都要有一句人话。"""
        assert set(_SENTENCES) == {
            "hotel.city.nightly_cap",
            "employee.level.known",
            "pricing.currency",
            "policy.effective_window",
        }

    def test_the_same_rule_in_two_cities_becomes_one_sentence(self) -> None:
        """多城行程一站一条证据；同一句话不该重复三遍。"""
        decision = PolicyDecision(
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
            tuple(
                RuleEvidence(
                    rule_id="hotel.city.nightly_cap",
                    actual=city,
                    threshold="configured city cap",
                    policy_version="gap-v1",
                    outcome=PolicyOutcome.INSUFFICIENT_EVIDENCE,
                    message="",
                    exception_allowed=False,
                )
                for city in ("Chengdu", "Hangzhou")
            ),
        )

        sentences = unjudged_gap_sentences(decision)

        assert len(sentences) == 1
        assert "Chengdu" in sentences[0] and "Hangzhou" in sentences[0]


class TestSelectingAnUnjudgedOption:
    def _task_with_an_unjudged_option(self):
        workflow, _ = build_demo_system(clock=lambda: FIXED_NOW)
        # 把上海的夜费上限拿掉——这就是广州、深圳、成都、杭州现在的处境。
        policy = replace(workflow.policies.current(), hotel_city_caps={})
        workflow.policies = type(workflow.policies)(
            (policy,), current_snapshot_id=policy.snapshot_id
        )
        task = workflow.create_task(make_demo_request(task_id="trip-unjudged"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
        )
        return workflow, task, option

    def test_the_traveler_sees_options_instead_of_a_dead_end(self) -> None:
        _, task, option = self._task_with_an_unjudged_option()

        assert task.state is TaskState.WAITING_FOR_USER
        assert option.option_id

    def test_choosing_it_needs_a_reason_and_goes_to_a_human(self) -> None:
        """判不了就交给人定——不是默默放行，也不是一句异常打死。"""
        workflow, task, option = self._task_with_an_unjudged_option()

        with pytest.raises(WorkflowError):
            workflow.select_option(task.task_id, option.option_id)

        task = workflow.select_option(
            task.task_id,
            option.option_id,
            business_reason="客户在这附近，先按这个报上去",
        )

        assert task.state is TaskState.WAITING_FOR_APPROVAL

    def test_the_approver_is_told_what_the_system_could_not_judge(self) -> None:
        """审批人要知道自己在批什么：批的是一条查不到标准，不是一条违规。"""
        workflow, task, option = self._task_with_an_unjudged_option()

        task = workflow.select_option(
            task.task_id, option.option_id, business_reason="客户在这附近"
        )

        assert task.approval is not None
        assert "unjudged:hotel.city.nightly_cap" in task.approval.violations
