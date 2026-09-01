"""工具循环：把"信息够不够"的判断从一张全局表挪到每个工具的签名上之后，
旧链路那句"哪天出发"应该消失，而所有硬边界一条都不许松。

这些测试用**脚本化的假模型**，不花钱、不碰真实 Provider。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.agent.tool_loop import (
    DEFAULT_TOOLS,
    ModelTurn,
    ToolExchange,
    ToolExecutor,
    ToolInputError,
    ToolInvocation,
    ToolLoopAborted,
    ToolLoopRunner,
    ToolSpec,
    _quote_fixes_date,
    _window_day_queries,
)
from corporate_travel_agent.domain.enums import TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
)
from corporate_travel_agent.policy.engine import PolicyEngine
from corporate_travel_agent.providers.base import TransportSearchQuery
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI)


def _transport(ref_id: str, depart: datetime, arrive: datetime, price: str) -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="catalog",
        provider="mock",
        mode=TransportMode.FLIGHT,
        origin="Beijing",
        destination="Shanghai",
        depart_at=depart,
        arrive_at=arrive,
        price=Decimal(price),
        seat_class="ECONOMY",
    )


@pytest.fixture
def executor() -> ToolExecutor:
    transports = [
        # 8月5日 08:35 到 —— 10 点前到得了
        _transport(
            "MU-EARLY",
            datetime(2026, 8, 5, 6, 20, tzinfo=SHANGHAI),
            datetime(2026, 8, 5, 8, 35, tzinfo=SHANGHAI),
            "950",
        ),
        # 13:40 到 —— 到不了，Provider 的时间窗会滤掉
        _transport(
            "MU-LATE",
            datetime(2026, 8, 5, 11, 30, tzinfo=SHANGHAI),
            datetime(2026, 8, 5, 13, 40, tzinfo=SHANGHAI),
            "820",
        ),
    ]
    hotels = [
        HotelOffer(
            ref_id="HT-1",
            snapshot_id="catalog",
            provider="mock",
            name="Bund Inn",
            city="Shanghai",
            check_in=date(2026, 8, 5),
            check_out=date(2026, 8, 6),
            nightly_price=Decimal("180"),
            commute_minutes=15,
        )
    ]
    return ToolExecutor(
        provider=MockProvider(transports, hotels, now=NOW),
        policy_engine=PolicyEngine(),
        employee=EmployeeProfileSnapshot(
            snapshot_id="emp-1",
            employee_id="E-1",
            level="IC",
            department="Sales",
            home_city="Beijing",
            manager_id="M-1",
            profile_version=1,
        ),
        policy=PolicySnapshot(
            snapshot_id="pol-1",
            policy_version="v1",
            level_rules={"IC": LevelTravelRule(("ECONOMY",), ("SECOND",))},
            hotel_city_caps={"Shanghai": Decimal("300")},
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        ),
        # 用真实的城市别名表：测试要走线上那条路，别自己造一份好走的。
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        now=NOW,
    )


class ScriptedModel:
    """按剧本依次给出一轮决定；记录每轮看到的 transcript。"""

    def __init__(
        self,
        script: Sequence[ToolInvocation | ModelTurn | Sequence[ToolInvocation]],
    ) -> None:
        self._script = list(script)
        self.seen: list[tuple[ToolExchange, ...]] = []

    def next_turn(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, object],
    ) -> ModelTurn:
        self.seen.append(tuple(transcript))
        if not self._script:
            raise AssertionError("剧本用完了，循环还在要下一步")
        item = self._script.pop(0)
        if isinstance(item, ModelTurn):
            return item
        if isinstance(item, ToolInvocation):
            return ModelTurn(calls=(item,))
        return ModelTurn(calls=tuple(item))


# ---------------------------------------------------------------------------
# 这条就是旧链路挂掉的那一条
# ---------------------------------------------------------------------------


def test_arrive_by_alone_is_enough_to_search(executor: ToolExecutor) -> None:
    """「8月5号从北京去上海，上午10点前到」不该再被问"哪天出发"。

    旧链路：模型判 READY 且字段全对，但那张必填表要一个独立的 departure_after，
    编译器不许猜 → 问出"哪天出发"，答案就写在原话里
    （reports/evaluation-runs/semantic-live-full-20260829-125000/）。
    """
    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {
                    "origin": "北京",
                    "destination": "上海",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                    # depart_after 没给 —— 用户原话里本来就没有这个东西
                },
            ),
            ToolInvocation(
                "propose_options",
                {"transport_refs": ["MU-EARLY"], "summary": "10点前到，符合政策"},
            ),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run(
        "8月5号从北京去上海，8月5日上午10点前到，不住酒店"
    )

    assert outcome.kind == "propose_options"
    assert outcome.transport_refs == ("MU-EARLY",)
    # 出发窗口是从时限往前算的，而且当面说了出来
    assert executor.assumptions == [
        "搜索窗口从 2026-08-04 16:00（Beijing 当地）起算，"
        "到达时限往前 18 小时，这样前一晚出发也能被搜到；"
        "因为你要求 08月05日 10:00 前到达"
    ]


def test_tight_deadline_window_opens_the_previous_evening(executor: ToolExecutor) -> None:
    """「10点前到」必须能搜到前一晚出发，不能钉死到达日当天 0 点。"""
    result = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日上午10点前到",
        }
    )
    depart_after = datetime.fromisoformat(result["depart_after"])
    arrive_by = datetime.fromisoformat(result["arrive_by"])
    assert depart_after.date() < arrive_by.date()
    assert depart_after.hour == 16
    assert "前一晚" in (result["assumed"] or "")


def test_previous_evening_flight_is_in_the_default_window() -> None:
    """前一晚那班必须落在默认窗口里，不能被到达日 0 点挡掉。"""
    extras = [
        _transport(
            "CA-EVE",
            datetime(2026, 8, 4, 19, 30, tzinfo=SHANGHAI),
            datetime(2026, 8, 4, 21, 50, tzinfo=SHANGHAI),
            "980",
        ),
        _transport(
            "MU-EARLY",
            datetime(2026, 8, 5, 6, 20, tzinfo=SHANGHAI),
            datetime(2026, 8, 5, 8, 35, tzinfo=SHANGHAI),
            "950",
        ),
    ]
    extra = ToolExecutor(
        provider=MockProvider(extras, [], now=NOW),
        policy_engine=PolicyEngine(),
        employee=EmployeeProfileSnapshot(
            snapshot_id="emp-1",
            employee_id="E-1",
            level="IC",
            department="Sales",
            home_city="Beijing",
            manager_id="M-1",
            profile_version=1,
        ),
        policy=PolicySnapshot(
            snapshot_id="pol-1",
            policy_version="v1",
            level_rules={"IC": LevelTravelRule(("ECONOMY",), ("SECOND",))},
            hotel_city_caps={"Shanghai": Decimal("300")},
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        ),
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        now=NOW,
        conversation="8月5号上午10点前要到上海",
    )
    result = extra.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5号上午10点前要到上海",
        }
    )
    refs = {option["ref_id"] for option in result["options"]}
    assert refs == {"CA-EVE", "MU-EARLY"}


def test_one_model_turn_can_call_two_tools(executor: ToolExecutor) -> None:
    """Codex 形状：一轮可以发出多个工具，不必每轮只调一个。"""
    model = ScriptedModel(
        [
            (
                ToolInvocation("lookup_city", {"name": "杭州"}),
                ToolInvocation(
                    "search_transport",
                    {
                        "origin": "北京",
                        "destination": "上海",
                        "arrive_by": "2026-08-05T10:00:00+08:00",
                        "date_evidence": "8月5号",
                    },
                ),
            ),
            ToolInvocation(
                "propose_options",
                {"transport_refs": ["MU-EARLY"], "summary": "两件工具同一轮"},
            ),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run(
        "8月5号从北京去上海，10点前到"
    )
    assert outcome.kind == "propose_options"
    first_turn = model.seen[1]
    assert len(first_turn) == 2
    assert {item.invocation.name for item in first_turn} == {
        "lookup_city",
        "search_transport",
    }


def test_a_message_with_no_tool_call_ends_the_loop(executor: ToolExecutor) -> None:
    """没有工具调用就是终局：已经搜到的方案跟着这句话一起交出去。"""
    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {
                    "origin": "北京",
                    "destination": "上海",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                },
            ),
            ModelTurn(calls=(), message="早班机 08:35 到，推荐这一班。"),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run(
        "8月5号从北京去上海，10点前到"
    )
    assert outcome.kind == "propose_options"
    assert "08:35" in (outcome.summary or "")
    assert "MU-EARLY" in outcome.transport_refs


def test_asking_after_a_search_keeps_the_found_options(executor: ToolExecutor) -> None:
    """Codex Default：提问不丢掉已经查到的货。"""
    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {
                    "origin": "北京",
                    "destination": "上海",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                },
            ),
            ToolInvocation(
                "ask_traveler",
                {"question": "杭州打算哪天去？"},
            ),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run(
        "8月5号从北京去上海，10点前到，之后还要去杭州"
    )
    assert outcome.kind == "propose_options"
    assert "MU-EARLY" in outcome.transport_refs
    assert outcome.open_questions == ("杭州打算哪天去？",)


def test_a_deadline_window_is_split_across_calendar_days() -> None:
    """Duffel 按出发日搜；跨日窗口必须拆成两次，前一晚和当天早都盖得到。"""
    query = TransportSearchQuery(
        origin="Beijing",
        destination="Shanghai",
        depart_after=datetime(2026, 8, 4, 16, 0, tzinfo=SHANGHAI),
        arrive_before=datetime(2026, 8, 5, 10, 0, tzinfo=SHANGHAI),
    )
    parts = _window_day_queries(query)
    assert len(parts) == 2
    assert parts[0].depart_after.date().isoformat() == "2026-08-04"
    assert parts[1].depart_after.date().isoformat() == "2026-08-05"
    assert parts[1].arrive_before == query.arrive_before


def test_search_tool_does_not_require_departure_time() -> None:
    """契约层面：这一格不在必填清单里，所以不可能因为它而卡住。

    必填清单里有 `date_evidence`，但它和旧链路那张表**不是一回事**：它要的是
    对话里已经有的原话，不是让旅行者再答一遍。答案在原文里就填得上，
    原文里没有就说明这一段的日期本来就不知道——那时候本来就该问。
    """
    spec = next(item for item in DEFAULT_TOOLS if item.name == "search_transport")
    assert "depart_after" not in spec.parameters["required"]
    assert "departure_after" not in spec.parameters["required"]
    assert "depart_after" in spec.parameters["properties"]


def test_model_sees_real_inventory_before_asking(executor: ToolExecutor) -> None:
    """提问是主动调用的工具，而且可以发生在看过真实库存之后。

    旧链路的追问是编译失败的副产品，只能拿字段名查一张中文字典，
    所以永远问不出"只有8:00那班，要吗"。
    """
    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {
                    "origin": "Beijing",
                    "destination": "Shanghai",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                },
            ),
            ToolInvocation(
                "ask_traveler",
                {"question": "10点前到上海的只有 08:35 抵达那班（950 USD，符合政策），按它订吗？"},
            ),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run("8月5号去上海，10点前要到")

    assert outcome.kind == "propose_options"
    assert "MU-EARLY" in outcome.transport_refs
    assert "08:35" in (outcome.open_questions[0] if outcome.open_questions else "")
    # 提问那一轮，模型手上已经有真实报价了
    inventory_turn = model.seen[-1]
    assert inventory_turn[-1].ok
    assert inventory_turn[-1].result["options"][0]["ref_id"] == "MU-EARLY"


# ---------------------------------------------------------------------------
# 硬边界：一条都不许松
# ---------------------------------------------------------------------------


def test_no_write_tool_is_reachable() -> None:
    """不许自动下单 = 工具清单里根本没有写操作。比开关硬。"""
    names = {spec.name for spec in DEFAULT_TOOLS}
    assert names == {
        "lookup_city",
        "search_transport",
        "search_hotels",
        "ask_traveler",
        "propose_options",
    }
    forbidden = {"book", "purchase", "pay", "issue_ticket", "create_order", "confirm_booking"}
    assert not (names & forbidden)


def test_every_option_carries_a_policy_verdict(executor: ToolExecutor) -> None:
    """模型拿不到没有政策标注的库存——政策附着在数据上，不是可跳过的一步。"""
    result = executor.search_transport(
        {
            "origin": "Beijing",
            "destination": "Shanghai",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日上午10点前到",
        }
    )
    assert result["options"]
    for option in result["options"]:
        assert option["policy_outcome"]
        assert "policy_violations" in option


def test_invented_reference_is_rejected(executor: ToolExecutor) -> None:
    """不许编造库存：没在本轮搜到过的 ref_id 不能交付。"""
    with pytest.raises(ToolInputError, match="不在本轮搜索结果里"):
        executor.validate_proposal({"transport_refs": ["MU-GHOST"], "summary": "编的"})


def test_past_deadline_is_refused(executor: ToolExecutor) -> None:
    """到达时限已经过去 —— 这趟不可能成立，工具入口直接拒。"""
    with pytest.raises(ToolInputError, match="已经过去"):
        executor.search_transport(
            {
                "origin": "Beijing",
                "destination": "Shanghai",
                "arrive_by": "2026-07-01T10:00:00+08:00",
                # 出处和日期要对得上，否则先被出处那道关卡拦住，验不到这一条。
                "date_evidence": "7月1号",
            }
        )


# ---------------------------------------------------------------------------
# 失败只废掉一次调用，不停整条链路
# ---------------------------------------------------------------------------


def test_bad_arguments_only_kill_one_call(executor: ToolExecutor) -> None:
    """参数错了，模型看见原因、改一次就能继续 —— 旧链路这里会整条停住。"""
    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {"origin": "Beijing", "destination": "Shanghai", "arrive_by": "下周三"},
            ),
            ToolInvocation(
                "search_transport",
                {
                    "origin": "Beijing",
                    "destination": "Shanghai",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                },
            ),
            ToolInvocation(
                "propose_options", {"transport_refs": ["MU-EARLY"], "summary": "改对了"}
            ),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run("8月5号去上海")

    assert outcome.kind == "propose_options"
    first = outcome.transcript[0]
    assert not first.ok
    assert first.result["field"] == "arrive_by"
    assert "ISO-8601" in first.result["error"]


def test_unknown_tool_name_is_recoverable(executor: ToolExecutor) -> None:
    """模型叫了个不存在的工具：把可用清单交回去，不是崩溃。"""
    model = ScriptedModel(
        [
            ToolInvocation("book_flight", {"ref": "MU-EARLY"}),
            ToolInvocation("ask_traveler", {"question": "确认一下日期？"}),
        ]
    )
    outcome = ToolLoopRunner(model=model, executor=executor).run("帮我订票")

    assert outcome.kind == "ask_traveler"
    assert not outcome.transcript[0].ok
    assert "book_flight" in outcome.transcript[0].result["error"]


def test_loop_is_bounded(executor: ToolExecutor) -> None:
    """模型不收敛时循环必须停 —— 预算耗尽是终止态，不是无限重试。"""
    model = ScriptedModel(
        [
            ToolInvocation("lookup_city", {"name": "Beijing"})
            for _ in range(10)
        ]
    )
    with pytest.raises(ToolLoopAborted, match="没有收敛"):
        ToolLoopRunner(model=model, executor=executor, max_iterations=3).run("在吗")


def test_invoke_hook_receives_every_call(executor: ToolExecutor) -> None:
    """宿主的工具预算/审计钩子要能罩住循环里的每一次调用。"""
    calls: list[tuple[str, str]] = []

    def invoke(*, tool_name: str, tool_kind: str, operation):
        calls.append((tool_name, tool_kind))
        return operation()

    model = ScriptedModel(
        [
            ToolInvocation(
                "search_transport",
                {
                    "origin": "Beijing",
                    "destination": "Shanghai",
                    "arrive_by": "2026-08-05T10:00:00+08:00",
                    "date_evidence": "8月5号",
                },
            ),
            ToolInvocation("propose_options", {"transport_refs": ["MU-EARLY"], "summary": "ok"}),
        ]
    )
    ToolLoopRunner(model=model, executor=executor, invoke=invoke).run("8月5号去上海10点前到")

    assert ("llm.next_tool_call", "LLM") in calls
    assert ("tool.search_transport", "PROVIDER") in calls


def test_a_repeated_search_is_refused_instead_of_burned(executor: ToolExecutor) -> None:
    """真模型实测的死法：一条航线没货，它原样重搜到预算耗尽，用户什么都没拿到。

    工具入口拒绝重复，模型才会去换条件或者去问人。
    """
    args = {
        "origin": "北京",
        "destination": "上海",
        "arrive_by": "2026-08-05T10:00:00+08:00",
        "date_evidence": "8月5号",
    }
    first = executor.search_transport(args)
    assert first["option_count"] == 1
    with pytest.raises(ToolInputError, match="已经做过"):
        executor.search_transport(args)


def test_an_empty_search_says_what_to_do_next(executor: ToolExecutor) -> None:
    """搜到 0 条时把下一步直说，别让模型自己猜——它猜出来的就是原样重试。"""
    result = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            # demo 库存里这天没有任何航班
            "arrive_by": "2026-08-09T10:00:00+08:00",
            "date_evidence": "8月9日",
        }
    )
    assert result["option_count"] == 0
    assert "重复同样的搜索不会有别的结果" in result["next_step"]


def test_a_naive_time_is_read_in_the_city_local_zone(executor: ToolExecutor) -> None:
    """"10 点前到上海"里的 10 点只有一个读法。

    真模型第一次常写成不带时区的 `2026-08-05T10:00:00`；打回去它补上 `+08:00`
    再试一遍，多花一次调用换来完全一样的结果。这是算术，直接算掉。
    """
    result = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00",
            "date_evidence": "8月5日上午10点前到",
        }
    )
    assert result["arrive_by"] == "2026-08-05T10:00:00+08:00"
    assert result["option_count"] == 1


def test_nudging_the_window_on_an_empty_route_is_also_refused(executor: ToolExecutor) -> None:
    """只拦"一模一样的重试"不够。

    真模型实测：一条航线没货，它把时间窗挪一小时再搜，参数不同就绕过了签名守卫，
    照样把 12 次预算烧光，最后什么都没告诉用户。连着搜空就该去问人。
    """
    for hour in (9, 10):
        result = executor.search_transport(
            {
                "origin": "北京",
                "destination": "上海",
                # demo 库存里这天没有任何航班
                "arrive_by": f"2026-08-09T{hour:02d}:00:00+08:00",
                "date_evidence": "8月9日",
            }
        )
        assert result["option_count"] == 0

    with pytest.raises(ToolInputError, match="ask_traveler"):
        executor.search_transport(
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "2026-08-09T11:00:00+08:00",
                "date_evidence": "8月9日",
            }
        )


def test_a_route_that_finds_something_is_not_penalised(executor: ToolExecutor) -> None:
    """搜空的计数只针对真的空的航线：搜到货就清零，别把正常行程也卡住。"""
    executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-09T09:00:00+08:00",
            "date_evidence": "8月9日",
        }
    )
    found = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5日",
        }
    )
    assert found["option_count"] == 1
    # 清零之后还能继续搜，不会被前一次的空结果连坐。
    again = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T13:00:00+08:00",
            "date_evidence": "8月5日",
        }
    )
    assert again["option_count"] >= 1


def _executor_with_conversation(conversation: str) -> ToolExecutor:
    """带对话原文的执行器：日期出处那道关卡要靠它核对。"""
    return ToolExecutor(
        provider=MockProvider([], [], now=NOW),
        policy_engine=PolicyEngine(),
        employee=EmployeeProfileSnapshot(
            snapshot_id="emp-1",
            employee_id="E-1",
            level="IC",
            department="Sales",
            home_city="Beijing",
            manager_id="M-1",
            profile_version=1,
        ),
        policy=PolicySnapshot(
            snapshot_id="pol-1",
            policy_version="v1",
            level_rules={"IC": LevelTravelRule(("ECONOMY",), ("SECOND",))},
            hotel_city_caps={},
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        ),
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        now=NOW,
        conversation=conversation,
    )


def test_a_date_nobody_stated_cannot_be_searched() -> None:
    """**这是"算 vs 猜"那条线在工具边界上的落点。**

    实测（`reports/evaluation-runs/toolloop-mc-*/`，MC-05）：旅行者只说了第一段的
    时间，"之后还要去杭州、再回北京"一个日期都没给，模型自己填了"当天 23:59"和
    "第二天 23:59"，当成确定行程交付。更糟的是 assumptions 写着"因为你要求 23:59
    前到达"——那个 23:59 是编的。

    关卡问的**不是**"这句话里有没有日期味儿的字"（那是关键词表，一改措辞就漏），
    而是"**这句话有没有定下你填进来的那一天**"。
    """
    executor = _executor_with_conversation(
        "8月5号上午10点前要到上海，我从北京走，之后还要去杭州"
    )

    # 一、引用是编的：对话里根本没有这句话。
    with pytest.raises(ToolInputError, match="逐字抄自对话原文"):
        executor.search_transport(
            {
                "origin": "上海",
                "destination": "杭州",
                "arrive_by": "2026-08-05T23:59:00+08:00",
                "date_evidence": "当天从上海去杭州",
            }
        )

    # 二、引用是真话，但那句真话没定下任何一天。
    with pytest.raises(ToolInputError, match="没有定下任何一天"):
        executor.search_transport(
            {
                "origin": "上海",
                "destination": "杭州",
                "arrive_by": "2026-08-05T23:59:00+08:00",
                "date_evidence": "之后还要去杭州",
            }
        )

    # 三、原话对得上就照常放行。
    ok = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5号上午10点前要到上海",
        }
    )
    assert ok["arrive_by"] == "2026-08-05T10:00:00+08:00"


def test_an_english_date_is_accepted_as_evidence() -> None:
    """§41.3 LT-03 的原话，逐字抄进出处。以前关卡不认 "Sept 9"，模型十种抄法全被拒。"""
    executor = _executor_with_conversation(
        "I need to fly from PEK to SHA on Sept 9, must land before noon"
    )
    result = executor.search_transport(
        {
            "origin": "PEK",
            "destination": "SHA",
            "arrive_by": "2026-09-09T12:00:00+08:00",
            "date_evidence": "fly from PEK to SHA on Sept 9, must land before noon",
        }
    )
    assert result["arrive_by"] == "2026-09-09T12:00:00+08:00"


def test_futile_evidence_retries_are_capped() -> None:
    """一个读不了的日期不许把预算烧光。

    实测（§41.3 LT-03）：出处关卡拒一次，模型就换一种抄法再试，10 轮全烧在同一段上，
    用户拿到"没有收敛"。被拒的调用从不登记签名，所以签名守卫拦不住它。
    现在同一条航线连着被拒超过 `_EVIDENCE_REFUSAL_LIMIT` 次，就换成硬话：
    交出已搜到的段，去问人。**能过关卡的引用照常放行，计数清零。**
    """
    executor = _executor_with_conversation(
        "8月5号上午10点前要到上海，我从北京走，之后还要去杭州，再回北京"
    )
    leg = {
        "origin": "上海",
        "destination": "杭州",
        "arrive_by": "2026-08-06T23:59:00+08:00",
    }
    # 前两次：逐条解释为什么这句话没定下哪一天
    for quote in ("之后还要去杭州", "再回北京"):
        with pytest.raises(ToolInputError, match="没有定下任何一天"):
            executor.search_transport({**leg, "date_evidence": quote})
    # 第三次起：不再解释，直说别再试、去问人
    with pytest.raises(ToolInputError, match="被拒 3 次") as excinfo:
        executor.search_transport({**leg, "date_evidence": "之后还要去杭州"})
    assert "ask_traveler" in str(excinfo.value)
    assert "propose_options" in str(excinfo.value)
    # 换了一条航线不受影响：计数按航线记
    with pytest.raises(ToolInputError, match="没有定下任何一天"):
        executor.search_transport(
            {
                "origin": "杭州",
                "destination": "北京",
                "arrive_by": "2026-08-07T23:59:00+08:00",
                "date_evidence": "再回北京",
            }
        )
    # 能过关卡的引用永远不拦——哪怕这条航线刚被拒过三次
    ok = executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5号上午10点前要到上海",
        }
    )
    assert ok["arrive_by"] == "2026-08-05T10:00:00+08:00"


def test_citing_one_day_and_searching_another_is_refused() -> None:
    """引用了 8 月 5 号却去搜 8 月 6 日——张冠李戴。

    这一类是"关键词表"那种做法**结构上抓不到**的：那句话里日期味儿的字一个不少。
    反过来验证（这一天在不在这句话里）才拦得住。
    """
    executor = _executor_with_conversation("8月5号从北京去上海开会")
    with pytest.raises(ToolInputError, match="对不上"):
        executor.search_transport(
            {
                "origin": "北京",
                "destination": "上海",
                "arrive_by": "2026-08-06T10:00:00+08:00",
                "date_evidence": "8月5号从北京去上海开会",
            }
        )


@pytest.mark.parametrize(
    ("quote", "resolved", "allowed"),
    [
        # 绝对日期：逐位比对
        ("8月5号上午10点前要到上海", "2026-08-05", True),
        ("8/5从北京去上海开会", "2026-08-05", True),
        ("8.5从北京去上海开会", "2026-08-05", True),
        ("2026.8.5从北京去上海", "2026-08-05", True),
        ("12月30日从北京去上海开会", "2026-12-30", True),
        # 跨年：年份归模型判，这里只比月日
        ("1月2日回", "2027-01-02", True),
        # 相对日期表达式：放行，具体哪一天的算术归模型（第 0 步实测它 8/8）
        ("下下周三从北京去上海开会", "2026-09-02", True),
        ("明天走", "2026-08-02", True),
        ("上海当天不过夜", "2026-08-06", True),
        # 只说了先后顺序，没说哪一天
        ("之后还要去杭州", "2026-08-05", False),
        ("再回北京", "2026-08-06", False),
        ("然后回北京", "2026-08-06", False),
        ("中间从上海到杭州我自己想办法", "2026-08-06", False),
        ("杭州帮我订一晚", "2026-08-06", False),
        # 农历没有固定公历日：本来就该问（和 H-04 同一个结论）
        ("春节从北京去上海出差", "2027-02-06", False),
        # 英文月份：§41.3 LT-03 的原话，之前关卡不认，模型换十种抄法烧光 10 轮
        ("fly from PEK to SHA on Sept 9, must land before noon", "2026-09-09", True),
        ("September 9th", "2026-09-09", True),
        ("Sep. 9, 2026", "2026-09-09", True),
        ("9 Sep", "2026-09-09", True),
        ("the 9th of September", "2026-09-09", True),
        ("Dec 30 out, Jan 2 back", "2027-01-02", True),
        # 英文月份同样逐位比对：引用 Sept 9 却搜 9 月 10 日，照样拦
        ("on Sept 9, must land before noon", "2026-09-10", False),
        # 月份词单独出现不算定下哪一天
        ("sometime in September", "2026-09-09", False),
    ],
)
def test_date_traceability_predicate(quote: str, resolved: str, allowed: bool) -> None:
    """判据本身的真值表，用的全是真实跑出来过的引用。"""
    moment = datetime.fromisoformat(f"{resolved}T10:00:00+08:00")
    assert (_quote_fixes_date(quote, moment) is None) is allowed


def test_a_partial_itinerary_is_a_valid_delivery(executor: ToolExecutor) -> None:
    """**交付和提问不是二选一。**

    这是这条链路最早的一个设计错误。三段行程里两段日期写在原话里、一段没写，
    模型只能选"全交付"或"全提问"，于是整轮停住，已经查到的两段一起丢了。
    同一个问题问 GPT，它把能定的排出来、只问缺的那段——那才是对的。
    """
    executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5号",
        }
    )
    refs = list(executor.seen_transport)
    transport, hotels, questions = executor.validate_proposal(
        {
            "transport_refs": refs,
            "summary": "北京→上海早班机 08:35 到，留足 1 小时余量",
            "open_questions": ["上海到杭州这一段，您希望哪天出发？"],
        }
    )
    assert transport == tuple(refs)
    assert hotels == ()
    assert questions == ("上海到杭州这一段，您希望哪天出发？",)


def test_a_complete_itinerary_carries_no_open_questions(executor: ToolExecutor) -> None:
    """全都定下来了就别硬凑问题——问题栏空着才表示这趟行程完整。"""
    executor.search_transport(
        {
            "origin": "北京",
            "destination": "上海",
            "arrive_by": "2026-08-05T10:00:00+08:00",
            "date_evidence": "8月5号",
        }
    )
    _, _, questions = executor.validate_proposal(
        {"transport_refs": list(executor.seen_transport), "summary": "早班机"}
    )
    assert questions == ()


def test_a_refused_search_leaves_no_assumption_behind() -> None:
    """**校验必须走在"记假设"前面。**

    端到端跑真模型时抓到的：先算出发时间、把假设记下来，再验日期出处。于是被出处
    关卡拒掉的那次搜索，假设还留在任务上——旅行者看到的是"因为你要求 23:59 前
    到达"，而他从没这么要求过。幻觉穿着推导的外衣，比直接报错难发现得多。
    """
    executor = _executor_with_conversation(
        "8月5号上午10点前要到上海，我从北京走，之后还要去杭州"
    )
    with pytest.raises(ToolInputError):
        executor.search_transport(
            {
                "origin": "上海",
                "destination": "杭州",
                "arrive_by": "2026-08-05T23:59:00+08:00",
                "date_evidence": "之后还要去杭州",
            }
        )
    assert executor.assumptions == []


def test_declared_requirements_must_use_the_supported_vocabulary(executor: ToolExecutor) -> None:
    hard, soft = executor.requirements_from(
        {"hard_constraints": ["direct_only", "direct_only"], "soft_preferences": ["prefer_train"]}
    )
    assert hard == ("direct_only",)
    assert soft == ("prefer_train",)
    assert executor.requirements_from({}) == ((), ())
    with pytest.raises(ToolInputError) as unknown_hard:
        executor.requirements_from({"hard_constraints": ["no_red_eye"]})
    assert unknown_hard.value.field_name == "hard_constraints"
    with pytest.raises(ToolInputError) as unknown_soft:
        executor.requirements_from({"soft_preferences": ["window_seat"]})
    assert unknown_soft.value.field_name == "soft_preferences"
    with pytest.raises(ToolInputError):
        executor.requirements_from({"hard_constraints": "direct_only"})


def test_hotel_required_needs_a_hotel_search_first(executor: ToolExecutor) -> None:
    with pytest.raises(ToolInputError) as refused:
        executor.requirements_from({"hard_constraints": ["hotel_required"]})
    assert refused.value.field_name == "hard_constraints"
    executor.search_hotels(
        {"city": "Shanghai", "check_in": "2026-08-05", "check_out": "2026-08-06"}
    )
    assert executor.requirements_from({"hard_constraints": ["hotel_required"]}) == (
        ("hotel_required",),
        (),
    )
