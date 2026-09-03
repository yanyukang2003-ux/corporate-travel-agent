"""tool_loop：把"信息够不够"的判断从一张全局表挪到每个工具自己的签名上。

## 换掉的是什么

旧语义链路是一条单向管道：模型抽一次意图 → 填一张必填表 → 表填满才准搜 →
表没填满就拿字段名去查一张中文字典生成追问。

那张表里有一格现实中不存在。用户说"8月5号从北京去上海，上午10点前到"，
`arrive_by` 有了，`departure_after` 空着——不是用户没说清楚，是**没有人会把
"当天10点前到"再拆成一个独立的出发时刻**。表填不满，于是问出"哪天出发"，
而答案就写在原话里。信息在填表那一刻就被压掉了，后面每一层都不知道它存在过。

这里换成有界工具循环：模型看着对话原文和前面所有工具的返回结果，自己决定
下一步——零个、一个或多个工具。**没有工具调用就是终局。** 每个工具在自己入口
校验自己需要的参数，校验不过只让这一次调用失败并把原因交回模型，整条链路不停。

## 硬边界没有变松，落点比以前更硬

- **不许自动下单**：工具清单是白名单，里面根本没有写操作。模型调不到，不靠开关。
- **政策由确定性代码判**：`PolicyEngine` 算完把结论贴在每条库存上一起返回。模型
  只能读，而且**拿不到没有政策标注的库存**。
- **不许编造库存**：`propose_options` 只接受本轮真实搜索返回过的 `ref_id`，
  编出来的引用直接拒绝。
- **模糊就问**：保留，但判据从"表填满没"变成模型看过真实库存之后的判断，
  而且提问本身是一个要显式调用的工具，不再是编译失败的副产品。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.constraints import (
    SUPPORTED_HARD_CONSTRAINTS,
    SUPPORTED_SOFT_PREFERENCES,
)
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    InventorySnapshot,
    PolicySnapshot,
    SearchProvenance,
    TransportOffer,
)
from corporate_travel_agent.policy.engine import PolicyEngine
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    TransportSearchQuery,
    TravelInventoryProvider,
)
from corporate_travel_agent.services.locations import (
    CityNormalizer,
    resolve_location_timezone,
)


class ToolInputError(ValueError):
    """工具入口拒绝了这次调用的参数。

    **这不是链路故障。** 它只让这一个工具调用失败，原因会原样交回模型，
    模型可以改参数重试、换工具，或者去问旅行者。旧链路里同样的情况会让
    整条编译停住并生成一句机械追问。
    """

    def __init__(self, message: str, *, field_name: str | None = None) -> None:
        super().__init__(message)
        self.field_name = field_name


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具对模型的完整契约：名字、说明、参数 schema。

    **这份 schema 就是关卡本身。** 旧链路靠一张全局必填表决定"能不能往下走"；
    这里由每个工具各自声明它需要什么，可不填的就标可不填并写清默认值怎么来。
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    terminal: bool = False

    def as_openai_tool(self) -> dict[str, Any]:
        """转成 OpenAI/Anthropic 通用的 function-calling 声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """一次工具调用：名字 + 参数。"""

    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """模型这一轮的决定：零个或多个工具调用，外加可选的一句话。

    **没有工具调用就是终局**——和 Codex / Claude / Grok-build 同一条退出条件。
    旧实现强迫每轮必须调一个工具，于是模型没法直接说话，只能走
    `ask_traveler` / `propose_options` 这两个伪装成工具的出口。
    """

    calls: tuple[ToolInvocation, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExchange:
    """一轮"模型调了什么 / 拿回了什么"，按原样喂回下一轮。

    失败也照样进 transcript：模型必须看见自己错在哪才能改。
    """

    invocation: ToolInvocation
    ok: bool
    result: Mapping[str, Any]


class ToolLoopAborted(RuntimeError):
    """循环没能收敛到终局动作（预算耗尽或超出最大轮数）。

    **带着 transcript 一起抛。** 循环没走完不代表它什么都没查到；把证据跟着
    失败一起丢掉，事后就没法知道它到底卡在哪一步。
    """

    def __init__(self, message: str, *, transcript: tuple[ToolExchange, ...] = ()) -> None:
        super().__init__(message)
        self.transcript = transcript


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """循环的终局。

    **"交付"和"提问"不是二选一。** 这是这条链路最早的一个设计错误：一趟行程里
    有三段，两段的日期写在原话里、一段没写，正确做法是**把能定的两段排出来，
    只问剩下那一段**——而不是整轮停住、什么都不给。

    `open_questions` 就是这个：方案照给，未决的事挂在旁边一起交出去。
    """

    kind: str  # "ask_traveler" | "propose_options"
    question: str | None = None
    #: 模型判定这条请求根本不是差旅安排。宿主据此落成 OUT_OF_SCOPE，而不是烧掉一轮澄清。
    #: 只有一段库存都没搜过时才有意义——搜过了就说明它自己也当成差旅在办。
    out_of_scope: bool = False
    summary: str | None = None
    transport_refs: tuple[str, ...] = ()
    hotel_refs: tuple[str, ...] = ()
    #: 已经排好的部分之外，还需要旅行者回答的事。空表示这趟行程完整了。
    open_questions: tuple[str, ...] = ()
    #: 旅行者说过的硬要求和偏好，用受支持的名字。**引用本身不带这些**：只交 ref_id
    #: 的话，"只要直飞"到了规划器就没了，"优先高铁"也不参与排序。
    hard_constraints: tuple[str, ...] = ()
    soft_preferences: tuple[str, ...] = ()
    transcript: tuple[ToolExchange, ...] = ()


@runtime_checkable
class ExchangeRecordingModel(Protocol):
    """可选能力：把每次模型往返的完整报文留下来（`OpenAIToolCallingLanguageModel.exchanges`）。

    过程记录靠它。评测替身不一定有，宿主用 isinstance 探测，没有就只记宿主自己看到的那部分。
    """

    exchanges: list[dict[str, Any]]


class ToolCallingLanguageModelPort(Protocol):
    """每轮决定下一步的语言模型端口。

    和旧的 `SemanticLanguageModelPort` 的区别：它不再被要求**一次**把整段
    意思压进一个结构体。和更早的 `next_tool_call` 的区别：一轮可以调零个、
    一个或多个工具；零个工具调用就是终局，控制权在模型，不在一张必填表。
    """

    def next_turn(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ModelTurn: ...


# ---------------------------------------------------------------------------
# 工具契约
#
# 注意每个 schema 的 required 列表：它就是这个工具的关卡。
# `search_transport` 不要 `depart_after`——知道"几点前到"就能算出搜索窗口，
# 这是算术不是猜测。窗口从时限**往前**扩，好让前一晚出发也被搜到。
# ---------------------------------------------------------------------------

#: 卡点承诺没给出发时刻时，搜索窗口从到达时限往前看这么久。
#: 18 小时覆盖"当天早班"和"前一晚出发过夜"两种走法，不替旅行者编一个日期。
_DEADLINE_LOOKBACK_HOURS = 18

_ISO_DATETIME = {
    "type": "string",
    # 实测模型会传 "23:59" 这种只有时间没有日期的值，被拒后重试，白花一轮。
    # 把"必须带日期"和反例一起写进 schema 说明里，比事后报错便宜。
    "description": (
        "完整的 ISO-8601 时刻，必须含日期，例如 2026-08-05T10:00:00+08:00。"
        "只写时间（如 23:59）会被拒绝。"
    ),
}

LOOKUP_CITY = ToolSpec(
    name="lookup_city",
    description=(
        "Resolve a city name into the canonical name this system searches with, plus "
        "its timezone. **Do not call this before searching.** search_transport and "
        "search_hotels normalize city names themselves — calling this first only burns "
        "your budget. Use it only after a search failed on a place name, or when a name "
        "is genuinely ambiguous."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "旅行者用的原话地名"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)

SEARCH_TRANSPORT = ToolSpec(
    name="search_transport",
    description=(
        "Search real transport inventory for one leg. Every returned option already "
        "carries this company's policy verdict — you may read it but never compute it. "
        "`depart_after` is optional: leave it out and the window opens "
        f"{_DEADLINE_LOOKBACK_HOURS} hours before `arrive_by`, so an evening-before "
        "departure is searchable when the traveler must be there by morning. Do not "
        "ask the traveler for a departure time you can derive from the deadline. "
        "`date_evidence` is required and must be COPIED VERBATIM from the conversation: "
        "the traveler's own words that fix THIS leg's date. If no words in the "
        "conversation fix this leg's date, you do not know it — propose the legs "
        "you could search and put this one in open_questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "arrive_by": _ISO_DATETIME | {"description": "最晚到达时刻（目的地当地）"},
            "depart_after": _ISO_DATETIME
            | {
                "description": (
                    "最早出发时刻；不填则从到达时限往前 "
                    f"{_DEADLINE_LOOKBACK_HOURS} 小时，覆盖前一晚出发"
                )
            },
            "date_evidence": {
                "type": "string",
                "description": "定下这一段日期的旅行者原话，必须逐字抄自对话",
            },
        },
        "required": ["origin", "destination", "arrive_by", "date_evidence"],
        "additionalProperties": False,
    },
)

SEARCH_HOTELS = ToolSpec(
    name="search_hotels",
    description=(
        "Search hotel inventory for one stay. Only call this when the traveler asked "
        "for lodging to be arranged — sleeping somewhere is a fact about the itinerary, "
        "booking a room is the traveler's decision."
    ),
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "check_in": {"type": "string", "description": "YYYY-MM-DD"},
            "check_out": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["city", "check_in", "check_out"],
        "additionalProperties": False,
    },
)

ASK_TRAVELER = ToolSpec(
    name="ask_traveler",
    description=(
        "Ask the traveler exactly one question. This does NOT discard searches you "
        "already ran — any options you found stay on the table, and the question is "
        "attached beside them. Call this only for a preference the traveler has not "
        "stated (Hangzhou dates, return day). Never for a fact you can derive or look "
        "up. Prefer propose_options with open_questions when you have anything to hand over."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "给旅行者看的问题原文"},
            "out_of_scope": {
                "type": "boolean",
                "description": (
                    "只在这条请求根本不是差旅安排（写周报、订外卖、问天气）时设为 true，"
                    "并在 question 里告诉旅行者。缺信息、有歧义都不是越界，不要设。"
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    terminal=True,
)

PROPOSE_OPTIONS = ToolSpec(
    name="propose_options",
    description=(
        "Hand the traveler what you actually found. Every ref_id must come from a search "
        "result in this conversation; invented references are rejected. "
        "**A partial itinerary is a valid delivery.** If you could settle two legs of a "
        "three-leg trip, propose those two and put the third in `open_questions` — do not "
        "throw away work you have already done just because one leg is still undecided. "
        "Prefer this over ask_traveler whenever you have anything worth handing over."
    ),
    parameters={
        "type": "object",
        "properties": {
            "transport_refs": {"type": "array", "items": {"type": "string"}},
            "hotel_refs": {"type": "array", "items": {"type": "string"}},
            "summary": {
                "type": "string",
                "description": "为什么是这些：给旅行者看的推荐理由，讲清取舍",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "这趟行程还没定下来的事，一条一个问题；全定了就留空",
            },
            "hard_constraints": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(SUPPORTED_HARD_CONSTRAINTS)},
                "description": (
                    "旅行者明确提出的硬要求（只要直飞、只坐高铁、必须订酒店……），"
                    "只能用列表里的名字。规划器按它过滤——ref_id 本身不带这些要求。"
                    "旅行者要的东西不在列表里，写进 open_questions 或 summary，别静默丢掉。"
                ),
            },
            "soft_preferences": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(SUPPORTED_SOFT_PREFERENCES)},
                "description": (
                    "旅行者说过的偏好（优先高铁、别太早、越便宜越好……），只能用列表里的名字。"
                    "规划器按它排序。"
                ),
            },
        },
        "required": ["transport_refs", "summary"],
        "additionalProperties": False,
    },
    terminal=True,
)

#: 模型能看见的全部工具。**这里面没有任何写操作**——这就是"不许自动下单"的落点。
DEFAULT_TOOLS: tuple[ToolSpec, ...] = (
    LOOKUP_CITY,
    SEARCH_TRANSPORT,
    SEARCH_HOTELS,
    ASK_TRAVELER,
    PROPOSE_OPTIONS,
)


class ToolExecutor:
    """工具的确定性实现：入口校验 → 调 Provider → 贴政策结论 → 返回。

    **模型永远拿不到没有政策标注的库存。** 政策不是一个模型可以选择跳过的步骤，
    它附着在数据上——这比"先搜、再规划、最后单独跑一次政策"硬，因为中间没有
    任何一层可以漏掉它。
    """

    def __init__(
        self,
        *,
        provider: TravelInventoryProvider,
        policy_engine: PolicyEngine,
        employee: EmployeeProfileSnapshot,
        policy: PolicySnapshot,
        city_normalizer: CityNormalizer,
        now: datetime,
        fallback_timezone: str = "Asia/Shanghai",
        conversation: str = "",
    ) -> None:
        self.provider = provider
        self.policy_engine = policy_engine
        self.employee = employee
        self.policy = policy
        self.city_normalizer = city_normalizer
        self.now = now
        self.fallback_timezone = fallback_timezone
        #: 对话原文，用来逐字核对每一段日期的出处。
        #:
        #: **这是"算 vs 猜"那条线在工具边界上的落点。** "8月5日10点前到"能算出出发
        #: 时间，因为答案唯一；"之后还要去杭州"算不出任何日期——实测模型会自己填
        #: 一个"当天 23:59"，再把它当成确定的行程交付。要求它说出原话，编的话对不上。
        self.conversation = conversation
        self.conversation_index = _searchable(conversation)
        #: 本轮真实见过的报价。`propose_options` 只认这里面的 ref_id——
        #: 这是"不许编造库存"的落点，和旧链路的证据校验同一个目的。
        self.seen_transport: dict[str, TransportOffer] = {}
        self.seen_hotels: dict[str, HotelOffer] = {}
        self.assumptions: list[str] = []
        #: 按调用顺序记下真实搜过的每一段/每一站（查询 + 快照）。
        #:
        #: 宿主靠它把循环的结果交给现成的规划器和证据校验，**不用再搜一遍**：
        #: 行程有几段是数出来的——数模型实际搜了几段，不是让它先声明。
        self.leg_searches: list[tuple[TransportSearchQuery, InventorySnapshot]] = []
        self.stay_searches: list[tuple[HotelSearchQuery, InventorySnapshot]] = []
        #: 每次搜索的出处，按发生顺序。宿主搜完会把它抄到任务上。
        self.searches: list[SearchProvenance] = []
        #: **每一次真实的**供应商搜索结果，未经合并——存档和溯源认这个。
        self.captured_snapshots: list[InventorySnapshot] = []
        #: 已经问过的搜索签名。**同样的问题问第二遍不会有新答案**，只会烧预算。
        #: 真模型实测就是这么烧光的：一条航线当天没货，它原样重搜六次，
        #: 直到工具预算耗尽——用户最后什么解释都没拿到。
        self._asked: set[str] = set()
        #: 每条航线搜空了几次。只拦"一模一样的重试"不够：实测模型会把时间窗
        #: 挪一小时再搜一遍，参数不同就绕过了签名守卫，照样把预算烧光。
        #: 一条航线连着搜空 `_EMPTY_ROUTE_LIMIT` 次，就该去问人，不是继续试。
        self._empty_routes: dict[str, int] = {}
        #: 每条航线的日期出处被拒了几次。被拒的调用从不登记签名（关卡先于签名守卫），
        #: 于是同一段可以换着抄法无限重试——实测（§41.3，LT-03）一个读不了的日期
        #: 把 10 轮预算全烧光，用户拿到一句"没有收敛"。超过 `_EVIDENCE_REFUSAL_LIMIT`
        #: 就换成硬话：交出已搜到的段，去问人。**能过关卡的引用永远不拦。**
        self._evidence_refusals: dict[str, int] = {}

    # -- 工具实现 ---------------------------------------------------------

    def lookup_city(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raw = _require_text(args, "name")
        canonical = self.city_normalizer.canonicalize(raw)
        timezone = resolve_location_timezone(canonical, fallback=self.fallback_timezone)
        return {
            "input": raw,
            "canonical_name": canonical,
            "timezone": timezone,
            "known": canonical.casefold() != raw.casefold() or timezone != self.fallback_timezone,
        }

    def search_transport(self, args: Mapping[str, Any]) -> dict[str, Any]:
        origin = self.city_normalizer.canonicalize(_require_text(args, "origin"))
        destination = self.city_normalizer.canonicalize(_require_text(args, "destination"))
        if origin.casefold() == destination.casefold():
            raise ToolInputError("出发地和目的地是同一座城市", field_name="destination")

        # 到达时刻按**目的地**当地读，出发时刻按**出发地**当地读——各自只有一个读法。
        destination_tz = resolve_location_timezone(
            destination, fallback=self.fallback_timezone
        )
        origin_tz = resolve_location_timezone(origin, fallback=self.fallback_timezone)
        arrive_by = _require_datetime(args, "arrive_by", assume_timezone=destination_tz)
        depart_after = _optional_datetime(
            args, "depart_after", assume_timezone=origin_tz
        )

        # ————————————————————————————————————————————————————————
        # **所有校验都要走在"记假设"前面。**
        #
        # 此前顺序反了：先算出发时间、把假设记下来，再验日期出处。于是被出处关卡
        # 拒掉的那次搜索，假设还留在任务上——旅行者看到的是"因为你要求 23:59 前
        # 到达"，而他从没这么要求过。**幻觉穿着推导的外衣**，比直接报错难发现得多。
        # ————————————————————————————————————————————————————————
        route = f"{origin}->{destination}"
        try:
            date_evidence = self._require_quoted_evidence(
                args, "date_evidence", resolved=arrive_by
            )
        except ToolInputError as exc:
            refusals = self._evidence_refusals.get(route, 0) + 1
            self._evidence_refusals[route] = refusals
            if refusals > _EVIDENCE_REFUSAL_LIMIT:
                raise ToolInputError(
                    f"{route} 这一段的日期出处已经被拒 {refusals} 次：对话里没有哪句话定下"
                    "这一天，或者系统读不了它的写法。**别再换抄法重试了**，每试一次都在烧预算。"
                    "现在做两件事：日期已经写明、已经搜到的段用 propose_options 交出去；"
                    "这一段用 ask_traveler 请旅行者用「9月9日」这样的写法确认日期。",
                    field_name="date_evidence",
                ) from exc
            raise
        self._evidence_refusals.pop(route, None)
        if arrive_by <= self.now:
            raise ToolInputError(
                f"到达时限 {arrive_by.isoformat()} 已经过去了（现在是 {self.now.isoformat()}）",
                field_name="arrive_by",
            )
        if depart_after is not None and arrive_by <= depart_after:
            raise ToolInputError(
                "到达时限不晚于最早出发时间，这条腿不可能成立", field_name="arrive_by"
            )

        note: str | None = None
        if depart_after is None:
            # ————————————————————————————————————————————————————————
            # 卡点承诺反推搜索窗口。这不是猜日期。
            #
            # 知道"8月5日10:00前必须到"，窗口从时限往前看 18 小时：当天早班和
            # 前一晚出发都能被搜到。钉死"到达日当天 00:00"会把前一晚那班挡在
            # 门外——那才是把算术做错了。旧链路更糟：这一格进了必填表，问出
            # "哪天出发"，答案就写在用户原话里。
            #
            # 算完当面说出来（记进 assumptions），旅行者看得见也能纠正。
            # ————————————————————————————————————————————————————————
            origin_zone = ZoneInfo(origin_tz)
            local_arrive = arrive_by.astimezone(origin_zone)
            depart_after = local_arrive - timedelta(hours=_DEADLINE_LOOKBACK_HOURS)
            now_local = self.now.astimezone(origin_zone)
            if depart_after < now_local:
                depart_after = now_local
            note = (
                f"搜索窗口从 {depart_after.strftime('%Y-%m-%d %H:%M')}（{origin} 当地）起算，"
                f"到达时限往前 {_DEADLINE_LOOKBACK_HOURS} 小时，这样前一晚出发也能被搜到；"
                f"因为你要求 {local_arrive.strftime('%m月%d日 %H:%M')} 前到达"
            )
            if note not in self.assumptions:
                self.assumptions.append(note)

        query = TransportSearchQuery(
            origin=origin,
            destination=destination,
            depart_after=depart_after,
            arrive_before=arrive_by,
        )
        self._refuse_repeat(
            f"transport|{route}|{depart_after.isoformat()}|{arrive_by.isoformat()}",
            "这次搜索刚刚已经做过了，结果不会变。",
        )
        if self._empty_routes.get(route, 0) >= _EMPTY_ROUTE_LIMIT:
            raise ToolInputError(
                f"{route} 这条航线已经试过 {_EMPTY_ROUTE_LIMIT} 个时间窗，都没有库存。"
                "**别再换时间窗试了**——用 propose_options 把已经查到的其他段交出去，"
                "把这一段放进 open_questions；没有其他段就用 ask_traveler 告诉旅行者。",
                field_name="destination",
            )
        # Duffel 一次只按一个出发日搜。窗口跨日时拆开再合并，模型仍只调一次工具。
        #
        # **合并只对模型和规划器成立，对审计不成立。** 合并出来的那个快照沿用第一天
        # 那次的 `snapshot_id` 和 `raw_payload_hash`，却装着第二天的报价——它会
        # *声称*自己是那些报价的证据，而它的原始响应里根本没有它们。事后按哈希去核，
        # 核不上。所以留下来存档的是**每一次真实搜索**，不是合并结果：每条报价
        # 指向真正产出它的那一次，那一次的原始响应里确实有它。
        parts = _window_day_queries(query)
        if len(parts) == 1:
            captured = [self.provider.search_transport(parts[0])]
        else:
            # 拆出的每一天是独立的供应商查询，串行纯属浪费：实测两天窗口 2.9s+2.3s。
            # 并行发出去，`map` 保序，快照顺序和串行完全一致——存档、溯源、合并都不用改。
            with ThreadPoolExecutor(max_workers=min(len(parts), 4)) as pool:
                captured = list(pool.map(self.provider.search_transport, parts))
        snapshot = captured[0] if len(captured) == 1 else _merge_transport_snapshots(captured)
        offers = [item for item in snapshot.items if isinstance(item, TransportOffer)]
        for offer in offers:
            self.seen_transport[offer.ref_id] = offer
        self.leg_searches.append((query, snapshot))
        self.captured_snapshots.extend(captured)
        # 出处和快照一起留下来。此前 `date_evidence` 验完就扔，于是事后能证明
        # "这张票来自哪个快照"，却证明不了"为什么搜的是这一天"——而后者才是
        # 用户会追问的那一句。见 `SearchProvenance`。
        #
        # 拆成几天搜的，就记几条：同一句原话、同一个窗口，但每一天各有自己的
        # 快照和原始响应，各自对得上。
        for part, part_snapshot in zip(parts, captured, strict=True):
            self.searches.append(
                SearchProvenance(
                    kind="transport",
                    parameters=(
                        ("origin", origin),
                        ("destination", destination),
                        ("depart_after", part.depart_after.isoformat()),
                        # 拆日的每一段都带到达时限（从 arrive_by 派生），这里只是把类型说清楚。
                        ("arrive_by", (part.arrive_before or arrive_by).isoformat()),
                        ("requested_window_arrive_by", arrive_by.isoformat()),
                    ),
                    snapshot_id=part_snapshot.snapshot_id,
                    query_hash=part_snapshot.query_hash,
                    captured_at=part_snapshot.captured_at,
                    valid_until=part_snapshot.valid_until,
                    date_evidence=date_evidence,
                    assumption=note,
                )
            )
        if offers:
            self._empty_routes.pop(route, None)
        else:
            self._empty_routes[route] = self._empty_routes.get(route, 0) + 1
        # 喂回模型的只取代表性子集：整页 24–49 条会把下一轮输入撑大一倍还多
        # （实测第二次调用 6k token 里大半是它）。**全部候选照样进规划器**——
        # 规划读的是快照，不是这份视图；最终方案里完全可能出现模型没看过的报价。
        shown = _representative_offers(offers)
        omitted = len(offers) - len(shown)
        next_step = _empty_result_advice(len(offers))
        if omitted > 0:
            next_step = (
                f"为控制上下文长度只列出 {len(shown)} 条（最便宜的和最早到的）；"
                f"另有 {omitted} 条同样进入了规划器，最终方案可能出现其中的报价。"
                "不要因为没看到某个时刻就重搜。" + (next_step or "")
            )
        return {
            "origin": origin,
            "destination": destination,
            "depart_after": depart_after.isoformat(),
            "arrive_by": arrive_by.isoformat(),
            "assumed": note,
            "snapshot_id": snapshot.snapshot_id,
            "valid_until": snapshot.valid_until.isoformat(),
            "options": [self._transport_view(offer) for offer in shown],
            "option_count": len(offers),
            "options_shown": len(shown),
            "options_omitted": omitted,
            "next_step": next_step,
        }

    def search_hotels(self, args: Mapping[str, Any]) -> dict[str, Any]:
        city = self.city_normalizer.canonicalize(_require_text(args, "city"))
        check_in = _require_date(args, "check_in")
        check_out = _require_date(args, "check_out")
        if check_out <= check_in:
            raise ToolInputError("退房日期必须晚于入住日期", field_name="check_out")

        query = HotelSearchQuery(city=city, check_in=check_in, check_out=check_out)
        self._refuse_repeat(
            f"hotel|{city}|{check_in.isoformat()}|{check_out.isoformat()}",
            "这次搜索刚刚已经做过了，结果不会变。",
        )
        snapshot = self.provider.search_hotels(query)
        offers = [item for item in snapshot.items if isinstance(item, HotelOffer)]
        for offer in offers:
            self.seen_hotels[offer.ref_id] = offer
        self.stay_searches.append((query, snapshot))
        self.captured_snapshots.append(snapshot)
        # 住宿日期没有单独的出处关卡——它跟着行程走。`date_evidence` 因此为 None，
        # **这是如实记录，不是漏了**：说不出出处的时候就不要假装说得出。
        self.searches.append(
            SearchProvenance(
                kind="hotel",
                parameters=(
                    ("city", city),
                    ("check_in", check_in.isoformat()),
                    ("check_out", check_out.isoformat()),
                ),
                snapshot_id=snapshot.snapshot_id,
                query_hash=snapshot.query_hash,
                captured_at=snapshot.captured_at,
                valid_until=snapshot.valid_until,
            )
        )
        return {
            "city": city,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "snapshot_id": snapshot.snapshot_id,
            "options": [
                self._hotel_view(offer)
                for offer in sorted(offers, key=lambda item: item.nightly_price)[:8]
            ],
            "option_count": len(offers),
            "options_shown": min(len(offers), 8),
            "options_omitted": max(len(offers) - 8, 0),
            "next_step": _empty_result_advice(len(offers)),
        }

    def validate_proposal(
        self, args: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """交付前的最后一道关：引用必须来自本轮真实搜到的库存。

        返回 `(交通引用, 酒店引用, 未决问题)`。未决问题不影响这一关的通过与否——
        **一份带着未决问题的部分行程仍然是合法交付**，只是它得说清楚还差什么。
        """
        transport_refs = _require_str_list(args, "transport_refs")
        hotel_refs = tuple(args.get("hotel_refs") or ())
        open_questions = tuple(
            str(item).strip() for item in (args.get("open_questions") or ()) if str(item).strip()
        )
        if not transport_refs:
            raise ToolInputError("至少要给出一条交通方案", field_name="transport_refs")
        unknown = [ref for ref in transport_refs if ref not in self.seen_transport]
        unknown += [ref for ref in hotel_refs if str(ref) not in self.seen_hotels]
        if unknown:
            raise ToolInputError(
                "这些引用不在本轮搜索结果里，不能交付："
                + "、".join(str(item) for item in unknown),
                field_name="transport_refs",
            )
        return transport_refs, tuple(str(item) for item in hotel_refs), open_questions

    def requirements_from(
        self, args: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """交付时声明的硬要求和偏好：只认词表里的名字，硬要求还得和搜过的东西对得上。

        名字不在词表里就拒绝这一次交付，理由交回模型——它该把那条要求写进
        open_questions 让旅行者知道系统做不到，而不是换个近义词硬塞进来。
        """
        hard = _optional_str_list(args, "hard_constraints")
        soft = _optional_str_list(args, "soft_preferences")
        unknown_hard = [item for item in hard if item not in SUPPORTED_HARD_CONSTRAINTS]
        if unknown_hard:
            raise ToolInputError(
                "hard_constraints 里有系统不认识的名字："
                + "、".join(unknown_hard)
                + "。支持的只有："
                + "、".join(sorted(SUPPORTED_HARD_CONSTRAINTS))
                + "。做不到的要求写进 open_questions 告诉旅行者。",
                field_name="hard_constraints",
            )
        unknown_soft = [item for item in soft if item not in SUPPORTED_SOFT_PREFERENCES]
        if unknown_soft:
            raise ToolInputError(
                "soft_preferences 里有系统不认识的名字："
                + "、".join(unknown_soft)
                + "。支持的只有："
                + "、".join(sorted(SUPPORTED_SOFT_PREFERENCES)),
                field_name="soft_preferences",
            )
        if "hotel_required" in hard and not self.stay_searches:
            raise ToolInputError(
                "声明了 hotel_required，却没有搜过酒店；先 search_hotels，或者不要声明它。",
                field_name="hard_constraints",
            )
        return tuple(dict.fromkeys(hard)), tuple(dict.fromkeys(soft))

    def _require_quoted_evidence(
        self, args: Mapping[str, Any], name: str, *, resolved: datetime
    ) -> str:
        """这一段的日期必须能在对话里找到出处，**而且那句话得真的定下这一天**。

        两道关，缺一不可：

        1. **是不是原话**——引用必须逐字来自对话。挡的是凭空造一句"当天从上海去杭州"。
        2. **这句原话定没定下这一天**——把模型填进来的日期拿回去和引用比对。
           挡的是引用了真话但那句话里没有日期（实测："之后还要去杭州"）。

        第二道**不是**在关键词表里找"日期味儿的字"，而是反过来验证：模型说这一天
        是从这句话来的，那这句话里就应该能找到这一天。找不到，就说明这一天是它自己
        想的。见 `_quote_fixes_date`。
        """
        quote = _require_text(args, name)
        if self.conversation_index and _searchable(quote) not in self.conversation_index:
            raise ToolInputError(
                f"{name} 必须逐字抄自对话原文，但对话里没有这句：{quote!r}。"
                "对话里没有哪句话定下这一段的日期，就说明你不知道它——"
                "用 ask_traveler 去问，别自己挑一个。",
                field_name=name,
            )
        verdict = _quote_fixes_date(quote, resolved)
        if verdict is not None:
            raise ToolInputError(verdict, field_name=name)
        return quote

    def _refuse_repeat(self, signature: str, message: str) -> None:
        """同一次搜索不做第二遍。

        这不是省钱，是**防死循环**：模型看不到新信息，就会把同一个动作重复到预算
        耗尽，而预算耗尽给用户的是一句"工具预算用完了"——比"这条航线当天没有货"
        差得多。拒绝它，模型才会去换条件或者去问人。
        """
        if signature in self._asked:
            raise ToolInputError(
                message + "换个时间窗或路线，或者用 ask_traveler 把情况告诉旅行者。"
            )
        self._asked.add(signature)

    # -- 政策标注 ---------------------------------------------------------

    def _transport_view(self, offer: TransportOffer) -> dict[str, Any]:
        decision = self.policy_engine.evaluate(self.employee, self.policy, [offer], None)
        return {
            "ref_id": offer.ref_id,
            "mode": offer.mode.value,
            "depart_at": offer.depart_at.isoformat(),
            "arrive_at": offer.arrive_at.isoformat(),
            "price": f"{offer.price} {offer.currency}",
            "seat_class": offer.seat_class,
            "is_direct": offer.is_direct,
            # 政策结论由确定性代码算好贴上来。模型只能读，不能算，也不能不看。
            "policy_outcome": decision.outcome.value,
            "policy_violations": list(decision.violation_ids),
        }

    def _hotel_view(self, offer: HotelOffer) -> dict[str, Any]:
        decision = self.policy_engine.evaluate(self.employee, self.policy, [], offer)
        return {
            "ref_id": offer.ref_id,
            "name": offer.name,
            "nightly_price": f"{offer.nightly_price} {offer.currency}",
            "nights": offer.nights,
            "commute_minutes": offer.commute_minutes,
            "policy_outcome": decision.outcome.value,
            "policy_violations": list(decision.violation_ids),
        }


# ---------------------------------------------------------------------------
# 参数解析：失败一律是 ToolInputError（只废掉这一次调用），不是链路异常
# ---------------------------------------------------------------------------


#: 一句话里出现这些之一，才算说了时间。
#:
#: 这份清单**宁可漏判也不误判**：漏判的后果是多问旅行者一句，误判的后果是
#: 拿一个编出来的日期去搜库存、还当成确定行程交付。两者不对等。
#: 绝对日期的写法：2026-08-05 / 2026年8月5日 / 8月5号 / 8/5 / 8.5 / 5号，
#: 以及英文月份：Sept 9 / September 9th / 9 Sep / Sep. 9（见 `_ENGLISH_DATE`）。
#: 抓到之后**和模型填进来的那一天逐位比对**——这是这道关卡真正的力气所在，
#: 它顺带还能挡住"引用了8月5号、却去搜8月6日"这类张冠李戴。
_ABSOLUTE_DATE = re.compile(
    # 四位年份那条要放在最前，分隔符要含 `.`：否则 "2026.8.5" 会被后一条从数字
    # 中间咬住，"26.8" 读成 26 月 8 日。`(?<!\d)` 保证月日那条不从数字中间起步。
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})"
    r"|(?<!\d)(?P<m2>\d{1,2})\s*[月/\-.]\s*(?P<d2>\d{1,2})\s*[号日]?"
    r"|(?<!\d)(?P<d3>\d{1,2})\s*[号日]"
)

#: 英文月份写法。实测（§41.3，LT-03）："fly from PEK to SHA on Sept 9" 模型读对了
#: 9 月 9 日，抄了原话当出处，关卡却不认 "Sept 9"——换十种抄法全被拒，10 轮烧完。
#: 坏的不是英文，是这张表。这里补上，**照样逐位比对月日**，不是放行。
_MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_BY_NAME = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ENGLISH_DATE = re.compile(
    # "Sept 9" / "Sept. 9th" / "September 9, 2026"（年份归模型，这里只比月日）
    rf"\b(?P<mon>{_MONTH_WORD})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b"
    # "9 Sep" / "9th of September"
    rf"|\b(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon2>{_MONTH_WORD})\b",
    re.IGNORECASE,
)

#: 相对日期的**表达式**（不是零散的字）。抓到就放行，不核对具体是哪一天——
#: 「下下周三是九月二号」那套算术归模型，第 0 步实测它在这上面 8/8。
#: 这里只回答一个更弱、也更好答的问题：**这句话到底有没有说时间。**
_RELATIVE_DATE = re.compile(
    r"今天|今日|明天|明日|后天|大后天|昨天|当天|同一天|次日|隔天|翌日"
    r"|周[一二三四五六日天末]|星期[一二三四五六日天]|礼拜[一二三四五六日天]"
    r"|下+个?[周星礼月]|这个?[周星礼月]|本[周月]|月底|月初|年底|年初"
    r"|today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday|next\s+(week|month|day)|this\s+(week|month)",
    re.IGNORECASE,
)


def _quote_fixes_date(quote: str, resolved: datetime) -> str | None:
    """这句引用有没有定下 `resolved` 这一天？定下了返回 None，否则返回拒绝理由。

    三种情况：

    - 引用里有**绝对日期**（8月5号、8/5、2026-08-05）→ 必须和 `resolved` 对得上。
      月日都写了就比月日；只写了"5号"就只比日。年份归模型（跨年规则另有一套）。
    - 引用里有**相对日期表达式**（明天、下下周三、当天）→ 放行，不核对哪一天。
      相对日期的算术归模型，这里只确认它确实说了时间。
    - **两样都没有**（"之后还要去杭州"、"再回北京"）→ 这句话只说了先后顺序，
      没说是哪一天。这一段的日期模型其实不知道，该去问。
    """
    local_date = resolved.date()
    months: list[tuple[int, int]] = []
    days: list[int] = []
    for match in _ABSOLUTE_DATE.finditer(quote):
        if match.group("y"):
            months.append((int(match.group("m")), int(match.group("d"))))
        elif match.group("m2"):
            months.append((int(match.group("m2")), int(match.group("d2"))))
        elif match.group("d3"):
            days.append(int(match.group("d3")))
    for match in _ENGLISH_DATE.finditer(quote):
        name = (match.group("mon") or match.group("mon2")).lower()
        months.append((_MONTH_BY_NAME[name], int(match.group("day") or match.group("day2"))))

    if months:
        if (local_date.month, local_date.day) in months:
            return None
        return (
            f"{quote!r} 里写的是 "
            + "、".join(f"{m}月{d}日" for m, d in months)
            + f"，但你要搜的是 {local_date.isoformat()}。"
            "两者对不上——要么改成引用里的那一天，要么这一段的日期另有出处，"
            "找不到出处就用 ask_traveler 去问。"
        )
    if days:
        if local_date.day in days:
            return None
        return (
            f"{quote!r} 里写的是 {days[0]} 号，但你要搜的是 {local_date.day} 号。"
            "对不上就别搜，用 ask_traveler 去问。"
        )
    if _RELATIVE_DATE.search(quote):
        return None
    return (
        f"{quote!r} 里没有定下任何一天。"
        "「之后」「再」「然后」只说了先后顺序，没说是哪一天——"
        f"{local_date.isoformat()} 是你自己挑的，不是旅行者说的。"
        "**别因此把整趟行程停住**：把日期已经写明的那几段照常搜完，"
        "用 propose_options 交出去，再把这一段放进 open_questions。"
    )


def _searchable(text: str) -> str:
    """把文本压成可比对的形式：去掉空白与常见标点差异，只留实词。

    逐字比对不是逐字节比对——模型抄原话时常会漏掉一个逗号或多一个空格，
    那不该被当成编造。真正要拦的是**对话里根本没有的日期**。
    """
    return "".join(
        ch for ch in text if not ch.isspace() and ch not in "，,。.、；;：:！!？?“”\"'（）()"
    )


#: 喂回模型的交通候选上限。规划器仍然看全部；这里只管模型的上下文。
_SHOWN_OFFER_LIMIT = 10


def _representative_offers(offers: list[TransportOffer]) -> list[TransportOffer]:
    """从全量候选里挑给模型看的子集：最便宜的 6 条 + 最早到的 4 条，按原顺序排。

    规则是确定性的、可解释的，而且写进了返回值的 `next_step`，模型知道自己看到的
    不是全部。挑法不追求最优——追求的是"模型要引用时手里有便宜的也有赶得上的"。
    """
    if len(offers) <= _SHOWN_OFFER_LIMIT:
        return list(offers)
    by_price = sorted(offers, key=lambda item: item.price)[:6]
    by_arrival = sorted(offers, key=lambda item: item.arrive_at)[:4]
    chosen = {offer.ref_id for offer in (*by_price, *by_arrival)}
    picked = [offer for offer in offers if offer.ref_id in chosen]
    return picked[:_SHOWN_OFFER_LIMIT]


#: 同一条航线连着搜空几次就不许再试。2 是有意留的余地：换一个时间窗是合理的
#: 第二次尝试，第三次就只是在烧预算了。
_EMPTY_ROUTE_LIMIT = 2

#: 同一条航线的日期出处连着被拒几次之后，不再逐条解释、改说硬话。2 同样是留的余地：
#: 第一次是抄错了句子，第二次可能是换了一句更准的；第三次还过不了，就不是抄法的问题。
_EVIDENCE_REFUSAL_LIMIT = 2


def _window_day_queries(query: TransportSearchQuery) -> tuple[TransportSearchQuery, ...]:
    """把跨日窗口拆成每天一段。供应商按出发日搜时，拆开才盖得住前一晚和当天早。"""
    if query.arrive_before is None:
        return (query,)
    start = query.depart_after.date()
    end = query.arrive_before.date()
    if end <= start:
        return (query,)
    tz = query.depart_after.tzinfo
    parts: list[TransportSearchQuery] = []
    day = start
    while day <= end and len(parts) < 3:
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = datetime.combine(day, time.max, tzinfo=tz)
        depart = max(query.depart_after, day_start)
        arrive = min(query.arrive_before, day_end)
        if arrive > depart:
            parts.append(
                TransportSearchQuery(
                    origin=query.origin,
                    destination=query.destination,
                    depart_after=depart,
                    arrive_before=arrive,
                )
            )
        day += timedelta(days=1)
    return tuple(parts) or (query,)


def _merge_transport_snapshots(
    snapshots: Sequence[InventorySnapshot],
) -> InventorySnapshot:
    """把按日拆开的搜索结果合成一次工具返回。"""
    if not snapshots:
        raise ValueError("cannot merge zero inventory snapshots")
    if len(snapshots) == 1:
        return snapshots[0]
    items: list[TransportOffer | HotelOffer] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for snapshot in snapshots:
        warnings.extend(snapshot.provider_warnings)
        for item in snapshot.items:
            if item.ref_id in seen:
                continue
            seen.add(item.ref_id)
            items.append(item)
    return replace(
        snapshots[0],
        items=tuple(items),
        provider_warnings=tuple(dict.fromkeys(warnings)),
        valid_until=max(item.valid_until for item in snapshots),
    )


def _empty_result_advice(count: int) -> str | None:
    """搜到 0 条时，把"接下来该干什么"直说，别让模型自己猜。"""
    if count:
        return None
    return (
        "这个时间窗和路线没有任何库存。**重复同样的搜索不会有别的结果。**"
        "已经查到的其他段照样交出去，把这一段放进 open_questions；"
        "没有其他段再问旅行者要不要换日期、换走法，或这一段自己安排。"
    )


def _require_text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"缺少参数 {name}", field_name=name)
    return value.strip()


def _require_datetime(
    args: Mapping[str, Any], name: str, *, assume_timezone: str | None = None
) -> datetime:
    parsed = _optional_datetime(args, name, assume_timezone=assume_timezone)
    if parsed is None:
        raise ToolInputError(f"缺少参数 {name}", field_name=name)
    return parsed


def _optional_datetime(
    args: Mapping[str, Any], name: str, *, assume_timezone: str | None = None
) -> datetime | None:
    """解析一个时刻；不带时区时按 `assume_timezone` 理解。

    **不带时区不算错。** "8月5日10点前到上海"里的 10 点只有一个读法——上海当地
    时间。实测真模型第一次常写成 `2026-08-05T10:00:00`，打回去它就补上 `+08:00`
    再试一遍：多花一次调用，换来完全一样的结果。这是算术，不是猜测，直接算掉。

    没给 `assume_timezone` 才退回报错——那种情况下确实没有唯一读法。
    """
    value = args.get(name)
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ToolInputError(
                f"{name} 不是合法的 ISO-8601 时刻：{value!r}", field_name=name
            ) from exc
    if parsed.tzinfo is None:
        if assume_timezone is None:
            raise ToolInputError(f"{name} 必须带时区", field_name=name)
        parsed = parsed.replace(tzinfo=ZoneInfo(assume_timezone))
    return parsed


def _require_date(args: Mapping[str, Any], name: str) -> date:
    value = args.get(name)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"缺少参数 {name}", field_name=name)
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolInputError(f"{name} 不是 YYYY-MM-DD：{value!r}", field_name=name) from exc


def _optional_str_list(args: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """可不填的字符串列表；填了就得是列表，里面全是非空字符串。"""
    value = args.get(name)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolInputError(f"{name} 必须是字符串列表", field_name=name)
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items):
        raise ToolInputError(f"{name} 里不能有空字符串", field_name=name)
    return items


def _require_str_list(args: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = args.get(name)
    if not isinstance(value, (list, tuple)):
        raise ToolInputError(f"{name} 必须是数组", field_name=name)
    return tuple(str(item) for item in value)


# ---------------------------------------------------------------------------
# 循环驱动
# ---------------------------------------------------------------------------


@dataclass
class ToolLoopRunner:
    """有界工具循环。

    每轮：模型看着对话原文 + 前面所有工具的返回结果，决定零个、一个或多个工具；
    非终局工具按顺序执行，结果（**成功和失败都算**）追加进 transcript 喂回下一轮。
    没有工具调用、或调用了终局工具，循环结束；否则直到预算/轮数用完。

    `invoke` 是给宿主注入 `TripWorkflowOrchestrator._invoke_tool` 的钩子——
    工具预算、审计轨迹、有界重试、并发闸全部沿用现成那一套，不另起炉灶。
    """

    model: ToolCallingLanguageModelPort
    executor: ToolExecutor
    tools: tuple[ToolSpec, ...] = DEFAULT_TOOLS
    max_iterations: int = 12
    invoke: Any = None  # (name, kind, callable) -> result；None 表示直接调

    def run(self, conversation: str, *, context: Mapping[str, Any] | None = None) -> LoopOutcome:
        transcript: list[ToolExchange] = []
        ctx = dict(context or {})

        for index in range(self.max_iterations):
            # ————————————————————————————————————————————————————————
            # 最后一轮**只给终局工具**。
            #
            # 真模型实测：一趟多城行程里两段没货，它就一条条换路线继续搜，直到预算
            # 见底，用户拿到的是一句"预算用完了"。加守卫拦得住某一种绕法，拦不住
            # 下一种——这是打地鼠。
            #
            # 收窄工具清单才是结构性的答案：**没得搜了，它只能交付或者开口问人。**
            # 边界还是在工具签名上，只不过这一轮的清单更短。
            # ————————————————————————————————————————————————————————
            last_round = index == self.max_iterations - 1
            offered = (
                tuple(spec for spec in self.tools if spec.terminal)
                if last_round
                else self.tools
            )
            # **收窄的是能调什么，不只是看得见什么。** 只改提供清单不够：实测模型
            # 在最后一轮照样调了 search_transport，而派发表里还有它，就真的执行了。
            by_name = {spec.name: spec for spec in offered}
            turn = self._call(
                "llm.next_tool_call",
                "LLM",
                lambda offered=offered: self._model_turn(
                    conversation, tuple(transcript), offered, ctx
                ),
            )
            if not turn.calls:
                # 没有工具调用 = 终局。和 Codex / Claude / Grok-build 同一条线。
                outcome = self._finish_from_message(turn.message, transcript)
                if outcome is not None:
                    return outcome
                transcript.append(
                    ToolExchange(
                        invocation=ToolInvocation("", {}),
                        ok=False,
                        result={"error": "这一轮既没有工具调用也没有可交付的话"},
                    )
                )
                continue

            non_terminal: list[tuple[ToolSpec, ToolInvocation]] = []
            terminals: list[tuple[ToolSpec, ToolInvocation]] = []
            for invocation in turn.calls:
                spec = by_name.get(invocation.name)
                if spec is None:
                    transcript.append(
                        ToolExchange(
                            invocation=invocation,
                            ok=False,
                            result={
                                "error": f"没有名为 {invocation.name} 的工具",
                                "available": sorted(by_name),
                            },
                        )
                    )
                    continue
                if spec.terminal:
                    terminals.append((spec, invocation))
                else:
                    non_terminal.append((spec, invocation))

            for spec, invocation in non_terminal:
                try:
                    result = self._call(
                        f"tool.{spec.name}",
                        "PROVIDER" if spec.name.startswith("search") else "LOCAL",
                        lambda name=spec.name, call=invocation: self._dispatch(
                            name, call.arguments
                        ),
                    )
                except ToolInputError as exc:
                    # 入口拒绝：只废掉这一次调用，原因原样交回模型。
                    transcript.append(
                        ToolExchange(
                            invocation=invocation,
                            ok=False,
                            result={"error": str(exc), "field": exc.field_name},
                        )
                    )
                    continue
                transcript.append(
                    ToolExchange(invocation=invocation, ok=True, result=result)
                )

            for spec, invocation in terminals:
                outcome = self._finish(spec, invocation, transcript)
                if outcome is not None:
                    return outcome

        raise ToolLoopAborted(
            f"{self.max_iterations} 轮内没有收敛到终局动作",
            transcript=tuple(transcript),
        )

    # -- 内部 -------------------------------------------------------------

    def _model_turn(
        self,
        conversation: str,
        transcript: tuple[ToolExchange, ...],
        offered: Sequence[ToolSpec],
        ctx: Mapping[str, Any],
    ) -> ModelTurn:
        """兼容只实现了旧 `next_tool_call` 的剧本模型。"""
        next_turn = getattr(self.model, "next_turn", None)
        if callable(next_turn):
            return next_turn(
                conversation=conversation,
                transcript=transcript,
                tools=offered,
                context=ctx,
            )
        invocation = cast(Any, self.model).next_tool_call(
            conversation=conversation,
            transcript=transcript,
            tools=offered,
            context=ctx,
        )
        return ModelTurn(calls=(invocation,))

    def _finish_from_message(
        self, message: str | None, transcript: list[ToolExchange]
    ) -> LoopOutcome | None:
        """模型这一轮选择直接说话。已经搜过的库存仍然必须带着走。"""
        text = (message or "").strip()
        refs = tuple(self.executor.seen_transport)
        hotels = tuple(self.executor.seen_hotels)
        if refs:
            return LoopOutcome(
                kind="propose_options",
                question=text or None,
                summary=text or "这是目前查到的方案",
                transport_refs=refs,
                hotel_refs=hotels,
                open_questions=(text,) if text else (),
                transcript=tuple(transcript),
            )
        if text:
            return LoopOutcome(
                kind="ask_traveler", question=text, transcript=tuple(transcript)
            )
        return None

    def _finish(
        self,
        spec: ToolSpec,
        invocation: ToolInvocation,
        transcript: list[ToolExchange],
    ) -> LoopOutcome | None:
        """终局工具也要过入口校验；不过就退回循环让模型重来。"""
        try:
            if spec.name == "ask_traveler":
                question = _require_text(invocation.arguments, "question")
                refs = tuple(self.executor.seen_transport)
                hotels = tuple(self.executor.seen_hotels)
                # Codex Default：提问不阻塞、不丢已经查到的货。
                if refs:
                    return LoopOutcome(
                        kind="propose_options",
                        question=question,
                        summary=question,
                        transport_refs=refs,
                        hotel_refs=hotels,
                        open_questions=(question,),
                        transcript=tuple(transcript),
                    )
                return LoopOutcome(
                    kind="ask_traveler",
                    question=question,
                    out_of_scope=bool(invocation.arguments.get("out_of_scope", False)),
                    transcript=tuple(transcript),
                )
            transport_refs, hotel_refs, open_questions = self.executor.validate_proposal(
                invocation.arguments
            )
            hard_constraints, soft_preferences = self.executor.requirements_from(
                invocation.arguments
            )
            return LoopOutcome(
                kind="propose_options",
                summary=_require_text(invocation.arguments, "summary"),
                transport_refs=transport_refs,
                hotel_refs=hotel_refs,
                open_questions=open_questions,
                hard_constraints=hard_constraints,
                soft_preferences=soft_preferences,
                transcript=tuple(transcript),
            )
        except ToolInputError as exc:
            transcript.append(
                ToolExchange(
                    invocation=invocation,
                    ok=False,
                    result={"error": str(exc), "field": exc.field_name},
                )
            )
            return None

    def _dispatch(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        handler = {
            "lookup_city": self.executor.lookup_city,
            "search_transport": self.executor.search_transport,
            "search_hotels": self.executor.search_hotels,
        }[name]
        return handler(args)

    def _call(self, tool_name: str, tool_kind: str, operation: Any) -> Any:
        if self.invoke is None:
            return operation()
        return self.invoke(tool_name=tool_name, tool_kind=tool_kind, operation=operation)
