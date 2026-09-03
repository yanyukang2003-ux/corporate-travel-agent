# 订好之后的追踪与变更（D20）评测报告

- 开始 2026-09-03T01:58:52.033567+00:00，结束 2026-09-03T02:03:13.895494+00:00，3 轮
- 装配：演示库存 + 钉住的时钟（2026-08-01）+ 内存仓储 + 进程内动态源；订了 300 趟
- 聊天场景模型：deepseek-v4-pro（提示词 tool-loop-v4）

## 按类汇总

| 类 | 条数 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 每轮都过 | 未全过的用例 |
|---|---|---|---|---|---|---|
| `delay_notify` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `dedupe` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `delay_small` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `delay_rebook` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `cancelled` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `return_leg` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `informational` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `webhook` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `worker` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `cancel_trip` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `events_api` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `chat_meeting_moved` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `chat_cancel` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `chat_flight_changed` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |
| `chat_ask` | 10 | 10/10 | 10/10 | 10/10 | 10/10 | — |

## 真模型开销

- 120 条聊天用例，120 次模型调用，输入 292215 / 输出 12316 token
- 价目表上界 $0.1378（cache-miss 口径）；单条中位耗时 2.11 秒

## 未通过的用例

无。

## 聊天用例明细（第 1 轮）

| 用例 | 原话 | 结果 | 实际 | 回复 / 说明 |
|---|---|---|---|---|
| `chat_move_01` | 客户把会议改到8月7号上午10点前了，帮我改一下 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-07T10:00:00+08:00 | 客户把会议改到8月7号上午10点前了，帮我改一下 |
| `chat_move_02` | 会议推迟到8月8日，下午2点前到就行 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-08T14:00:00+08:00 | 会议推迟到8月8日，下午2点前到就行 |
| `chat_move_03` | 改成8月6号早上9点到 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-06T09:00:00+08:00 | 改成8月6号早上9点到 |
| `chat_move_04` | Meeting moved to Aug 9, I need to arrive by 11am | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-09T11:00:00+08:00 | Meeting moved to Aug 9, I need to arrive by 11am |
| `chat_move_05` | 会议提前到8月4号下午4点 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-04T16:00:00+08:00 | 会议提前到8月4号下午4点 |
| `chat_move_06` | 8/7 10:00 前到上海就行 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-07T10:00:00+08:00 | 8/7 10:00 前到上海就行 |
| `chat_move_07` | 会议改到2026年8月10日中午12点前 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-10T12:00:00+08:00 | 会议改到2026年8月10日中午12点前 |
| `chat_move_08` | 返程改一下，8月7号晚上10点前到北京就行 | ✅ | 200 change=True kind=MEETING_MOVED leg=1 got=2026-08-07T22:00:00+08:00 | 返程改一下，8月7号晚上10点前到北京就行 |
| `chat_move_09` | 客户说改到7号上午十点 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-07T10:00:00+08:00 | 客户说改到7号上午十点 |
| `chat_move_10` | 帮我把到达时间改到8月8号上午9点半前 | ✅ | 200 change=True kind=MEETING_MOVED leg=0 got=2026-08-08T09:30:00+08:00 | 帮我把到达时间改到8月8号上午9点半前 |
| `chat_cancel_01` | 这趟不去了 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_02` | 行程取消，项目黄了 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_03` | 不用去上海了，把这趟差旅取消 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_04` | Cancel this trip, the client postponed indefinitely | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_05` | 老板说不去了 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_06` | 取消吧 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_07` | 我请假了，这次出差不去了 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_08` | 这趟差旅作废 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_09` | 客户取消会议，行程取消 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_cancel_10` | 不去了，退票我自己办 | ✅ | 200 trip=CANCELLED change=False | 已按你的话取消这趟差旅，管理端和谁在哪看板都不再显示它。已经订好的票请到官方平台自行退改——本系统不代退、不代改。 |
| `chat_flight_01` | 航司短信说我8月5号那班取消了 | ✅ | 200 change=True excluded=['MU-EARLY'] | 航司短信说我8月5号那班取消了 |
| `chat_flight_02` | 我的去程航班被取消了 | ✅ | 200 change=True excluded=['MU-EARLY'] | 我的去程航班被取消了 |
| `chat_flight_03` | 东航通知航班取消，帮我重新订 | ✅ | 200 change=True excluded=['MU-EARLY'] | 东航通知航班取消，帮我重新订 |
| `chat_flight_04` | My outbound flight got cancelled by the airline | ✅ | 200 change=True excluded=['MU-EARLY'] | My outbound flight got cancelled by the airline |
| `chat_flight_05` | 刚收到通知8月6号回北京的航班取消了，换一班 | ✅ | 200 change=True excluded=['MU-RETURN'] | 刚收到通知8月6号回北京的航班取消了，换一班 |
| `chat_flight_06` | 航空公司说去上海这班不飞了 | ✅ | 200 change=True excluded=['MU-EARLY'] | 航空公司说去上海这班不飞了 |
| `chat_flight_07` | 返程那张票被航司取消了，重新找一班 | ✅ | 200 change=True excluded=['MU-RETURN'] | 返程那张票被航司取消了，重新找一班 |
| `chat_flight_08` | 去程航班取消，需要改签 | ✅ | 200 change=True excluded=['MU-EARLY'] | 去程航班取消，需要改签 |
| `chat_flight_09` | 航司把我8月5号早上的航班砍了 | ✅ | 200 change=True excluded=['MU-EARLY'] | 航司把我8月5号早上的航班砍了 |
| `chat_flight_10` | 收到取消短信，回程没了，帮我看别的航班 | ✅ | 200 change=True excluded=['MU-RETURN'] | 收到取消短信，回程没了，帮我看别的航班 |
| `chat_ask_01` | 情况有变 | ✅ | 200 trip=BOOKED change=False | 您说"情况有变"，能具体说一下是什么变化吗？比如会议时间改了、航班被取消/改签了，还是整个行程要取消？ |
| `chat_ask_02` | 会议可能要改时间 | ✅ | 200 trip=BOOKED change=False | 会议要改到什么时候？请告诉我新的日期和时间（以及是改去程还是回程的到场时限），我才能帮你调整行程。 |
| `chat_ask_03` | 帮我订个酒店 | ✅ | 200 trip=BOOKED change=False | 好的，可以帮您订酒店。请问您需要哪一晚的住宿？是 8 月 5 日入住、8 月 6 日退房（在上海住一晚）吗？另外有没有预算或位置偏好（比如靠近某个地点）？ |
| `chat_ask_04` | 航班会不会延误？ | ✅ | 200 trip=BOOKED change=False | 您问的是航班会不会延误——这个我无法预测，也无法查询实时航班动态。请问您是想对已订行程做什么调整吗？比如改签、取消，还是会议时间有变动？ |
| `chat_ask_05` | 改到下周 | ✅ | 200 trip=BOOKED change=False | 您说“改到下周”，但没有说明具体是哪一天、以及改的是哪一段（去程、回程，还是两段都改）。请告诉我新的日期。 |
| `chat_ask_06` | 能不能换个时间 | ✅ | 200 trip=BOOKED change=False | 您想改哪一段的时间，以及改成什么时间呢？（比如第 0 段北京→上海，或第 1 段上海→北京，以及新的日期/时刻） |
| `chat_ask_07` | 帮我写个周报 | ✅ | 200 trip=BOOKED change=False | 这条请求不是差旅安排（写周报），我这边只能处理已订行程的变更（会议改期、航班变动、取消行程）。需要我帮你改行程吗？ |
| `chat_ask_08` | 我想改一下 | ✅ | 200 trip=BOOKED change=False | 您想改哪方面呢？比如会议时间变了、某段航班被航司取消/改期，还是整个行程要取消？请告诉我具体是哪一段、改成什么。 |
| `chat_ask_09` | 客户那边有变动 | ✅ | 200 trip=BOOKED change=False | 客户那边具体是什么变动？比如会议时间改了（请告诉我新的日期/时间），还是航班被取消/改签了，还是整个行程要取消？ |
| `chat_ask_10` | 延后两天 | ✅ | 200 trip=BOOKED change=False | 您说“延后两天”，是指会议/到场时间整体延后两天（即第 0 段需在 2026-08-08 10:00 前到达，第 1 段相应延后），还是仅某一段行程延后？请确认。 |

## 判据

- `delay_notify` / `dedupe`：去程延误但新到达仍早于 08-06 09:00（时限减 60 分钟缓冲）→ `NOTIFY_ONLY`，差旅仍 `BOOKED`，审计和发件箱各 +1；同一条再报一次不再 +1。
- `delay_small`：变化 ≤ 10 分钟 → `NO_CHANGE`，不进发件箱。
- `delay_rebook` / `cancelled`：`REBOOK_REQUIRED`，开改期任务，被影响的票在 `excluded_refs` 且不在候选里，原任务仍 `BOOKING_CONFIRMED`，事件带评估。
- `return_leg`：返程窗口到 23:00、不留缓冲，10 条混合期望。
- `informational`：查不到 / 已起飞 / 已落地 / 按计划 → 什么都不做。
- `webhook`：无密钥 503、坏签名 401、缺签名 401、签的是别的正文 401、票号没人盯 404、坏 JSON 422、非法状态 422、多余字段 422、按票号命中、按 trip_id 命中并开改期任务。
- `worker`：只领到点的差旅、按动态源判、领完释放租约并重排。
- `cancel_trip`：各状态可取消、二次取消 409、取消后事件 / 动态 / 聊天一律拒绝、看板不显示、任务状态不动。
- `events_api`：会议改期改指定段并平移出发窗口、越界 409、缺参数 409、航变手工报、改期中再报 409。
- `chat_*`：真模型把话读成变更请求：会议改期要对上日期和哪一段；取消要让差旅 `CANCELLED`；航变自述要排除对的那张票；该问的时候问、行程不动。
