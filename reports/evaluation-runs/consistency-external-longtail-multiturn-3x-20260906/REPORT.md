# 三轮一致性 · 比决策不比文本 · D19 多轮续问 185 条 × 3 轮（2026-09-02 的三轮报告）

- runner: `run-consistency-v1` · generated_at `2026-09-06T15:56:56.039656+00:00`
- 轮次：`external-longtail-multiturn-live-r1-20260902`、`external-longtail-multiturn-live-r2-20260902`、`external-longtail-multiturn-live-r3-20260902`
- 三轮共有用例：185

## 先说名词

| 词 | 大白话 |
|---|---|
| 一致 | 同一句话三轮重跑做了同样的事；这里比的是决策，不比措辞 |
| 终局决策 | 一条用例最后落在哪：追问 / 出方案 / 越界 / 诚实失败 / 供应商失败 |
| 搜索签名 | 一次搜索的路线加旅行者给的到达日；跨日拆窗和被拒的重复搜索得到同一个签名 |
| 追问目标 | 追问在问哪件缺的事（日期、住宿……），用关键词规则读出来，是粗版 |
| 参照 | 报告自带的对错依据：单轮集是离线替身的终态，多轮集是事实表说这条能不能办成 |

## 结论

| 层 | 结果 | 口径 |
|---|---|---|
| 第 1 层 · 终局决策 | 172/185 = 93.0% | 三轮 `outcome` 相同的用例比例 |
| 第 2 层 · 搜索参数 | 139/143 = 97.2% | 在 2 轮里都搜了的用例中，去重后路线和日期完全相同的比例（去重前 133）；比较方式 `parameters` |
| 第 3 层 · 声明的硬要求 | 未计算 | 现有报告没有落盘，要先改 runner 再重跑 |
| 第 4 层 · 追问目标 | 139/177 = 78.5% | 三轮都追问了的用例中，问的核心事实集合相同的比例（全集相同 96）；关键词粗版 |

## 第 1 层 · 终局决策

| 桶 | 数 | 含义 |
|---|---:|---|
| `consistent_and_matches_reference` | 156 | 三轮相同，且与参照一致（一致且对） |
| `consistent_but_differs_from_reference` | 16 | 三轮相同，但与参照不一致（一致但错，要人看） |
| `mixed` | 13 | 三轮不同 |

各轮终局分布：

| 轮 | completed | honest_failure | out_of_scope | stuck_clarifying |
|---|---:|---:|---:|---:|
| `external-longtail-multiturn-live-r1-20260902` | 130 | 47 | 7 | 1 |
| `external-longtail-multiturn-live-r2-20260902` | 133 | 48 | 3 | 1 |
| `external-longtail-multiturn-live-r3-20260902` | 135 | 46 | 3 | 1 |

三轮不同的 13 条：

| case_id | external-longtail-multiturn-live-r1-20260902 | external-longtail-multiturn-live-r2-20260902 | external-longtail-multiturn-live-r3-20260902 |
|---|---|---|---|
| `ct-human-h20241029143455115600` | out_of_scope | out_of_scope | completed |
| `ct-human-h20241029143508251643` | out_of_scope | completed | completed |
| `ct-human-h20241029143512507336` | honest_failure | completed | completed |
| `ct-human-h20241029143606240237` | honest_failure | completed | completed |
| `ct-human-h20241029143630351554` | honest_failure | completed | completed |
| `ct-human-h20241029143657365639` | completed | completed | honest_failure |
| `ct-human-h20241029143709097610` | completed | honest_failure | completed |
| `ct-human-h20241029143722458026` | completed | honest_failure | honest_failure |
| `ct-human-h20241029143732290071` | honest_failure | honest_failure | completed |
| `ct-human-h20241029143735439292` | out_of_scope | completed | completed |
| `ct-human-h20241029143913181191` | out_of_scope | honest_failure | honest_failure |
| `ad-val-0032` | completed | honest_failure | completed |
| `ad-val-0036` | out_of_scope | completed | out_of_scope |

## 第 2 层 · 搜索参数

跳过的轮次：`external-longtail-multiturn-live-r1-20260902`（report.json 没有 searches 字段）。只在其余轮次之间比。

| 桶 | 数 |
|---|---:|
| `identical` | 139 |
| `searched_in_some_compared_runs_only` | 3 |
| `routes_differ` | 4 |
| `search_presence_mixed`（比较的轮次里有的搜了有的没搜） | 3 |

被宿主拒掉的搜索尝试（`ToolInputError`，不算搜了）：

| 轮 | 次数 | 涉及用例 |
|---|---:|---:|
| `external-longtail-multiturn-live-r1-20260902` | 118 | 95 |
| `external-longtail-multiturn-live-r2-20260902` | 116 | 92 |
| `external-longtail-multiturn-live-r3-20260902` | 104 | 89 |

去重后仍不同的用例（每轮的签名集合）：

- `ct-human-h20241029143542560410` · `routes_differ`
  - `external-longtail-multiturn-live-r2-20260902`: transport | Nanjing | Guangzhou | 2026-09-03
  - `external-longtail-multiturn-live-r3-20260902`: transport | Guangzhou | Nanjing | 2026-09-07; transport | Nanjing | Guangzhou | 2026-09-03
- `ct-human-h20241029143630351554` · `routes_differ`
  - `external-longtail-multiturn-live-r2-20260902`: transport | Chengdu | Chongqing | 2026-09-23; transport | Chongqing | Chengdu | 2026-09-24
  - `external-longtail-multiturn-live-r3-20260902`: hotel | Chongqing | 2026-09-05 | 2026-09-07; transport | Chengdu | Chongqing | 2026-09-05
- `ad-val-0007` · `routes_differ`
  - `external-longtail-multiturn-live-r2-20260902`: transport | EWR | PHX | 2027-02-28; transport | JFK | PHX | 2027-02-28; transport | LGA | PHX | 2027-02-28; transport | PHX | EWR | 2027-02-26; transport | PHX | JFK | 2027-02-26; transport | PHX | LGA | 2027-02-26
  - `external-longtail-multiturn-live-r3-20260902`: transport | LGA | PHX | 2027-02-28; transport | PHX | LGA | 2027-02-26
- `ad-val-0031` · `routes_differ`
  - `external-longtail-multiturn-live-r2-20260902`: transport | Denver | New York | 2027-03-22; transport | New York | Denver | 2027-03-20
  - `external-longtail-multiturn-live-r3-20260902`: transport | DEN | JFK | 2027-03-22; transport | JFK | DEN | 2027-03-20

## 第 4 层 · 追问目标（粗版）

方法：`keyword-rules-v1`。核心事实（date, origin, destination, arrive_by）在整段话里出现即算问了；附带事实只在带问号或「吗 / 还是 / 需不需要」的句子里才算。

三轮都追问了的用例 177；核心事实集合一致 139；全集一致 96；只有附带事实不同 43；有的轮追问有的轮没有 3（与第 1 层的不一致重叠）。

| 问的事 | 至少一轮问了 | 三轮一致 | 一致率 |
|---|---:|---:|---:|
| `date` | 168 | 161 | 91.0% |
| `origin` | 12 | 168 | 94.9% |
| `destination` | 17 | 166 | 93.8% |
| `arrive_by` | 20 | 160 | 90.4% |
| `lodging` | 54 | 130 | 73.5% |
| `transport_mode` | 34 | 144 | 81.4% |
| `travelers` | 0 | 177 | 100.0% |
| `budget` | 11 | 166 | 93.8% |
| `intent_confirm` | 12 | 165 | 93.2% |

核心事实不同的 38 条（需人看是真差异还是关键词漏判）：

- `ct-human-h20241029143508251643`
  - `external-longtail-multiturn-live-r1-20260902` [lodging]：你问的是成都适合和朋友一起玩的地方（景点/玩法推荐），这属于旅游攻略，不是差旅安排（订机票/酒店/高铁）。我可以帮你查南京到成都的交通和成都的酒店，但景点推荐我这边没有工具能查。需要我帮你安排交通和住宿吗？
  - `external-longtail-multiturn-live-r2-20260902` [date]：你想让我帮你安排从南京到成都的交通（3人）吗？另外，成都3天游的出发日期是哪天？告诉我日期我就能帮你查车票/机票。
  - `external-longtail-multiturn-live-r3-20260902` [date, intent_confirm, lodging]：您这趟是想让我帮您安排从南京到成都的交通（以及可能的住宿），还是想先了解成都适合和朋友一起玩的地方/景点推荐？另外，方便告诉我大概的出发日期吗？
- `ct-human-h20241029143509607671`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date]：你打算哪天出发去上海？漫展具体是哪一天（需要哪天到场）？告诉我日期我就能把苏州→上海的车票/机票一起搜出来。
  - `external-longtail-multiturn-live-r2-20260902` [date]：你还没说具体哪天出发、哪天返回（漫展是哪一天？）。告诉我日期，我就能帮你查苏州到上海的交通班次；酒店我已经按 9/3 入住、9/5 退房查好了，等你确认日期后一起给你。
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你计划哪天出发去上海？（漫展的具体日期是哪天？我好按日期查车票和酒店）
- `ct-human-h20241029143522848652`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date]：开会具体是几点？这决定选哪一班（10:10 落地 vs 更晚的班次）。 返程日期是哪天？四天行程（9月3日到）通常 9月6日返深，需要你确认。 美食广场和大学有具体想去的地方吗？北京高校集中在海淀（清华、北大），美食广场可选王府井小吃街或合生汇等，我可以据此规划路线。
  - `external-longtail-multiturn-live-r2-20260902` [date, destination]：返程日期未定：四天行程，是9月6日还是9月7日从北京回深圳？需要我帮你查返程航班吗？ 美食广场和大学具体想去哪几个？北京有簋街、王府井小吃街、五道口（清华北大附近）等，告诉我偏好我可以帮你规划路线。
  - `external-longtail-multiturn-live-r3-20260902` [date, destination]：四天里哪几天是开会、哪几天有空闲，方便我帮你把美食广场和大学的具体路线排进去？ 大学想去哪一所（北大/清华/人大等），还是都想看看？
- `ct-human-h20241029143530430866`
  - `external-longtail-multiturn-live-r1-20260902` [date]：请问您计划哪天从深圳出发去北京？（旅行天数5天，我需要出发日期才能帮您查交通和住宿）
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, lodging, origin, transport_mode]：我来帮您规划这趟深圳到北京的亲子游。先确认一下行程安排：  - **出发地**：深圳 - **目的地**：北京 - **人数**：3人（您和父母） - **天数**：5天  不过有几个关键信息我还需要向您确认，才能开始搜索：  1. **出发日期**：您计划哪天出发？（比如"9月10日出发"） 2. **交通偏好**：深圳到北京距离较远，您倾向坐飞机还是高铁？考虑到老人旅途不要太辛苦，飞机可能更省
  - `external-longtail-multiturn-live-r3-20260902` [date]：您还没有告诉我出发日期。请问您计划哪天从深圳出发去北京？（旅行天数5天，我可以据此帮您安排往返和行程节奏。）
- `ct-human-h20241029143533982475`
  - `external-longtail-multiturn-live-r1-20260902` [intent_confirm]：你这次主要是想让我帮你安排"成都→北京→巴塞罗那"的交通（机票/转机），还是只需要北京停留三天的免费景点建议？另外，你希望我帮你查一下成都到北京、以及北京到巴塞罗那的航班吗？
  - `external-longtail-multiturn-live-r2-20260902` [date, intent_confirm]：你这次主要是想让我帮你安排成都→北京→巴塞罗那的交通（含北京大兴机场附近三天的停留），还是只想要北京大兴机场附近免费景点的推荐？另外，北京停留的三天具体是哪几天（比如几号到几号）？
  - `external-longtail-multiturn-live-r3-20260902` [lodging]：抱歉，我需要先说明一下：您这条请求主要是想了解北京大兴机场附近有哪些**免费且交通便利的景点**，这属于旅游景点推荐，而不是我可以帮您安排的差旅预订（机票、酒店、火车票等）。  不过我可以直接告诉您一些信息，同时说明我的边界：  **大兴机场附近免费景点（供参考，非预订服务）：**  1. **大兴机场本身** — 机场航站楼本身就是个"景点"，有"凤凰展翅"造型、中轴线景观、免税店和艺术装置，免
- `ct-human-h20241029143538018046`
  - `external-longtail-multiturn-live-r1-20260902` [destination, lodging]：你这条消息里同时出现了成都和重庆两个目的地，我想先确认一下：这趟行程是「南京出发去成都玩，中间在重庆停留一天」，还是「南京出发去重庆玩一天」？另外，你提到的是「推荐重庆最值得去的地方」，这属于景点/游玩推荐，不是订机票、高铁或酒店——我可以帮你查交通和住宿，但景点推荐不在我的工具范围内。你能先告诉我具体想让我帮你安排哪一段的交通或住宿吗？
  - `external-longtail-multiturn-live-r2-20260902` [intent_confirm, lodging]：你这条请求是想让我安排南京→重庆→成都的交通和住宿，还是只想让我推荐重庆一天里最值得去的景点？如果是后者，我可以直接给你推荐，不需要订票订房。
  - `external-longtail-multiturn-live-r3-20260902` [intent_confirm, lodging]：你这条请求是想让我安排南京→重庆→成都的交通和住宿，还是只想让我推荐重庆一天值得去的景点？如果是后者，我可以直接给你推荐，不需要订票订房。
- `ct-human-h20241029143542560410`
  - `external-longtail-multiturn-live-r1-20260902` [intent_confirm]：您是想让我帮您安排南京到广州的交通（明天出发），还是只想了解广州有什么好玩的景点推荐？
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging]：回程日期：5天行程，是9月7日还是9月8日回南京？需要我帮你查返程票吗？ 住宿：需要我帮你订广州的酒店吗（9月3日入住，住几晚）？
  - `external-longtail-multiturn-live-r3-20260902` [lodging]：返程想坐哪个时刻的航班？ 是否需要我帮你订广州的酒店？
- `ct-human-h20241029143543943871`
  - `external-longtail-multiturn-live-r1-20260902` [budget, date, destination, lodging, origin, transport_mode]：我先确认一下这趟行程的关键信息，再帮你查交通和住宿。  目前我理解到的： - 出发地：成都 - 目的地：北京 - 人数：4人 - 时长：5天左右 - 兴趣：名校氛围（清华、北大）+ 历史文化（故宫、长城、胡同等）  不过有几个信息我还需要你确认，才能帮你查可预订的交通和酒店：  1. **出发日期**：你们打算哪天从成都出发？（比如 9月几号） 2. **交通偏好**：成都到北京，你们倾向坐高铁还
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging, transport_mode]：我先确认一下：这是一次从成都到北京的4人5天游玩行程，属于差旅安排，我可以帮你查交通和住宿。  不过有几个关键信息需要你补充，我才能开始搜索：  1. **出发日期**：你们打算哪天从成都出发？（5天行程，我需要知道具体日期才能查票） 2. **住宿**：需要我帮忙订北京的酒店吗？（4人，5天） 3. **交通偏好**：成都到北京，你们倾向坐高铁还是飞机？有没有"必须直飞"或"必须高铁"这类硬性要
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你们计划哪天从成都出发去北京？（比如「9月10号出发」或「下周三出发」都可以）
- `ct-human-h20241029143604280402`
  - `external-longtail-multiturn-live-r1-20260902` [（没读出在问什么）]：您说的"端午假期三天"具体是哪三天出发和返回？2026年端午节是6月19日（周五），端午假期通常是6月19日—6月21日。请问您是打算6月19日从杭州出发去上海，6月21日（第三天）晚上19点前返回杭州吗？
  - `external-longtail-multiturn-live-r2-20260902` [date]：您说的"端午假期三天"具体是哪三天？2026年端午节是6月19日（周五），假期通常是6月19日-21日。您希望哪天从杭州出发去上海，第三天（6月21日）晚上19点前回到杭州，对吗？
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：端午假期三天具体是哪三天出发？2026年端午节是6月19日（周五），三天假期通常是6月19日—6月21日。请问您是6月19日从杭州出发去上海，第三天（6月21日）晚上19点前返回杭州吗？
- `ct-human-h20241029143625307702`
  - `external-longtail-multiturn-live-r1-20260902` [date]：您计划哪天从北京出发去上海？（停留3天，我需要出发日期才能查交通和酒店）另外说明：我的工具只能查交通和酒店库存，无法搜索米其林三星等餐厅推荐，这部分需要您自行查询或我无法提供。
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, origin]：您具体哪天从北京出发去上海？停留3天是从哪天到哪天？告诉我出发日期，我就能帮您搜北京→上海的交通（高铁/航班）。
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你计划哪天从北京出发去上海？（停留3天，我需要出发日期才能查询交通和酒店）
- `ct-human-h20241029143628653733`
  - `external-longtail-multiturn-live-r1-20260902` [date, origin]：您开头写的是"当前位置南京、目标位置上海"，但正文又说"从北京出发去上海"。请问出发地到底是北京还是南京？另外，您希望哪天出发、哪天返回（3天左右的具体日期）？
  - `external-longtail-multiturn-live-r2-20260902` [date]：出发城市是北京还是南京？（你开头写"当前位置南京"，正文又写"从北京出发"，两者不一致）另外，3天行程具体哪天出发？
  - `external-longtail-multiturn-live-r3-20260902` [date, origin]：你的出发地是北京还是南京？（正文写"从北京出发"，但位置信息显示"当前位置南京"）另外，你计划哪天出发去上海？
- `ct-human-h20241029143632058747`
  - `external-longtail-multiturn-live-r1-20260902` [（没读出在问什么）]：您提到在北京转机、想安排一天行程，但这条请求更像是当地游玩建议而非差旅预订（订机票/火车票/酒店）。请问您需要我帮您安排的是：重庆到北京、以及北京出发的交通票务，还是只需要北京一日游的景点推荐？
  - `external-longtail-multiturn-live-r2-20260902` [date, destination]：您这次是转机路过北京，想安排一天的城市游览。请问您需要我帮忙安排的是：1）重庆到北京的交通（以及后续转机离开北京的交通），还是 2）只帮您规划北京一天值得去的景点行程？另外，您转机去往的目的地是哪里、大概哪天到北京？
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, lodging, transport_mode]：您提到在北京转机、想安排一天行程探索景点，但这条请求里没有给出任何可搜索的差旅要素（出发/到达日期、航班或高铁班次、是否需要订酒店）。请问您需要我帮您安排的是：① 重庆—北京的交通（哪天出发、几点前要到北京）？还是 ② 北京当地的酒店？景点游览本身不在我的差旅搜索范围内，我可以帮您查交通和住宿，但具体"最值得去的地方"需要您自己定或另找攻略。
- `ct-human-h20241029143706437033`
  - `external-longtail-multiturn-live-r1-20260902` [date]：您说想去重庆玩四天，但没有说具体哪一天出发。请问您计划哪天从南京出发（比如 9 月几号）？我好按这个日期帮您查高铁往返票。
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, origin]：您还没有告诉我具体的出发日期，所以我暂时无法搜索高铁票。不过我可以先把行程框架和预算帮您理清楚，等您告诉我出发日期，我立刻去查往返高铁票。  先说明一下我目前掌握的信息和需要您确认的一点：  **已明确的：** - 出发地：南京 → 目的地：重庆 - 人数：2 人 - 天数：4 天 - 交通：必须高铁（G 字头）往返 - 想去的景点：三峡博物馆、重庆动物园、朝天门、人民大礼堂 - 想体验重庆本地饮
  - `external-longtail-multiturn-live-r3-20260902` [date]：您想去重庆玩四天，但还没有告诉我具体是哪一天出发。高铁往返的日期需要您先定下来，我才能帮您查票。  请问您计划**哪一天从南京出发**去重庆？（比如 9月10日出发、9月13日返回，或者您方便的具体日期）
- `ct-human-h20241029143824748448`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date, intent_confirm]：您这次是让我安排上海到南京的交通和行程，还是主要想了解南京适合拍照打卡的网红景点推荐？另外，一日游的话，您希望哪天出发、大概几点到南京、几点返回上海？
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging]：您这次是问南京的网红拍照打卡景点推荐，还是需要我帮您安排上海到南京的往返交通（以及住宿）？如果是后者，请告诉我出发日期和大概的返程时间。
  - `external-longtail-multiturn-live-r3-20260902` [date]：您这次是想要我帮您安排上海到南京的交通（往返车票/机票）吗？还是主要想了解南京适合拍照打卡的网红景点推荐？另外，一日游的话，您计划哪天出发、当天往返吗？
- `ct-human-h20241029143832205713`
  - `external-longtail-multiturn-live-r1-20260902` [date]：中秋节的具体日期我需要先确认一下。2026年的中秋节是9月25日（农历八月十五）。不过您说的"中秋节"具体是哪一天出发，我需要跟您确认一下，因为中秋假期通常有几天。  让我先确认一下：您计划中秋节当天（9月25日）出发，还是中秋假期期间出发？另外，关于海洋馆和动物园，广州有这些选择：  - **海洋馆**：广州正佳极地海洋世界（市区，适合带孩子）、广州海洋馆 - **动物园**：广州长隆野生动物世
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：您说的"中秋节"出发，是指中秋节当天（2026年9月25日，周五）出发，还是想在中秋假期（9月25日-27日）的某一天出发？另外，两个孩子想去的海洋馆和动物园，您希望我帮您查一下广州有哪些推荐吗？
  - `external-longtail-multiturn-live-r3-20260902` [date]：您说的"中秋节"具体是哪一天出发？2026年中秋节是9月25日（周五），中秋假期通常是9月25日-27日。您希望哪天从杭州飞广州？
- `ct-human-h20241029143837173547`
  - `external-longtail-multiturn-live-r1-20260902` [date, lodging, origin, transport_mode]：这次广州团建，我需要先确认几个关键信息才能帮您安排：1）出发日期是哪天（5天行程从哪天开始）？2）交通方面，杭州到广州是希望坐高铁还是飞机，或者两者都看看？3）住宿需要我帮您一起安排吗（5人、5天）？
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging, transport_mode]：这趟杭州→广州的团建，我需要先确认几个关键信息才能帮你查票：1）出发日期是哪天（5天行程，哪天出发）？2）是否需要我帮你订酒店，还是只查交通？3）交通上有没有偏好（比如优先高铁/飞机、只要直飞等）？
  - `external-longtail-multiturn-live-r3-20260902` [date, lodging]：这趟杭州→广州的团建，我需要先确认几个关键信息才能帮你查票：出发日期是哪天（或哪天必须到广州）？另外，住宿需要我一起安排吗（5人5天）？
- `ct-human-h20241029143839056472`
  - `external-longtail-multiturn-live-r1-20260902` [lodging]：我先确认一下：您是从南京出发去上海，用3天时间游览上海的高校，对吗？还是说南京和上海的高校都要游览？  另外，您希望我帮您安排交通（南京→上海）和住宿，还是只需要游览路线建议？  请告诉我这两点，我好为您规划。
  - `external-longtail-multiturn-live-r2-20260902` [date]：您想游览高校，但这条请求里没有给出任何具体的出发日期或需要到达的时间。请问您计划哪天出发、哪天返回？（例如：9月5日出发，9月7日返回）
  - `external-longtail-multiturn-live-r3-20260902` [date]：您想游览高校，但这条请求里没有给出出发日期和返程日期。请问您计划哪天出发、哪天返回？（另外，您说的"三天之内游览高校"是指在上海游览高校，还是南京、上海两地的高校都要安排？）
- `ct-human-h20241029143840382242`
  - `external-longtail-multiturn-live-r1-20260902` [date, origin]：您想哪天从南京出发去北京？5天行程大概从哪天开始（比如9月几号出发）？
  - `external-longtail-multiturn-live-r2-20260902` [date, origin]：您计划哪天从南京出发去北京？5天行程大概想从哪天开始（比如9月几号出发）？
  - `external-longtail-multiturn-live-r3-20260902` [date, lodging]：我来帮你规划这趟南京→北京的5天历史文化之旅。先确认一下：你提到"旅行天数5天"，但没有说具体出发日期。我需要知道出发日期才能搜索交通和住宿。  另外，关于住宿——你是希望我帮你安排酒店，还是只规划交通和行程？  先告诉我出发日期吧，这样我就能开始搜索了。
- `ct-human-h20241029143904148477`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, budget, date]：我已经查到了北京的酒店，但出发日期还需要你确认一下。  关于行程，我先说明几点：  **日期问题**：你说"去北京玩五天"，但没有说具体哪天出发。我需要你告诉我出发日期（比如"9月10号出发"），我才能查南京到北京的交通。  **预算**：3人、5天、2间房，预算小于10000元。我按这个先查了酒店（假设9月3日入住、9月8日退房，共5晚），找到几家交通便利的：  | 酒店 | 每晚价格(USD)
  - `external-longtail-multiturn-live-r2-20260902` [date]：请问你们计划哪一天从南京出发去北京？（比如具体日期，我好帮你查当天的车票/机票和酒店）
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问您计划哪天从南京出发去北京？（5天行程，我需要出发日期才能查交通和订房）
- `ct-human-h20241029143906048411`
  - `external-longtail-multiturn-live-r1-20260902` [date]：你们打算哪天出发去上海？玩3天的话，请告诉我出发日期（比如9月几号），我好帮你查往返的车票/机票。
  - `external-longtail-multiturn-live-r2-20260902` [date]：请问您计划哪天出发去上海？（例如：9月5日出发，玩3天）
  - `external-longtail-multiturn-live-r3-20260902` [budget, date, destination, lodging]：我先确认一下：您是从南京出发去上海玩3天，3个人，预算5000以内，偏好现代繁华的地方。不过您还没有告诉我出发日期和返回日期，这决定了我能查到哪些实际的车票/机票。  在您补充日期之前，我先说明一下我能帮您做的：  1. **交通**：南京→上海（去程）、上海→南京（返程），我可以查高铁/航班的实际班次和价格。 2. **住宿**：您提到"玩3天"，如果需要我帮您订酒店，请告诉我；如果只是行程建议
- `ct-human-h20241029143913181191`
  - `external-longtail-multiturn-live-r1-20260902` [date, intent_confirm, lodging]：这趟是问南京的景点推荐，不是要我帮你订车票或酒店，对吗？如果是想让我安排苏州到南京的交通或住宿，告诉我出发和返回的日期，我就能帮你查。
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, intent_confirm, lodging]：这趟是问南京的景点推荐，不是要我帮你订车票/酒店，对吧？如果是想让我安排苏州到南京的往返交通和住宿，告诉我你们打算哪个周末出发、大概几点到南京，我就能帮你查票。
  - `external-longtail-multiturn-live-r3-20260902` [transport_mode]：这趟是问南京的景点推荐，不是要我帮你订车票或酒店。请问你需要我帮你查苏州到南京的交通（高铁/火车）吗？还是只想先了解南京有哪些值得逛的江南水乡景点？
- `ad-val-0004`
  - `external-longtail-multiturn-live-r1-20260902` [date]：I need to know the date of the festival before I can search for flights. Could you tell me which day the pioneer festival takes place (or the date you need to arrive in Detroit by)?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：What date does the pioneer festival take place (and do you need to arrive by a specific time that day)?
  - `external-longtail-multiturn-live-r3-20260902` [date]：What date does the pioneer festival take place (or what date do you need to arrive in Detroit by)?
- `ad-val-0007`
  - `external-longtail-multiturn-live-r1-20260902` [date, destination, transport_mode]：Hello David! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for t
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：Hello David! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260902` [date, destination, transport_mode]：Hello David! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for t
- `ad-val-0008`
  - `external-longtail-multiturn-live-r1-20260902` [date, destination, transport_mode]：Hello Gary! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences (direct flights, train vs. plane, etc.)?
  - `external-longtail-multiturn-live-r2-20260902` [destination]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r3-20260902` [destination]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
- `ad-val-0010`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date, transport_mode]：I'd be happy to help you book a flight, Anna. Could you tell me a bit more about your trip?  - Where are you flying **from** and **to**? - What **date** do you need to travel (or do you need to arrive
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, origin]：I'd be happy to help you book a flight, Anna. To search for options, I need to know a few details about your trip:  1. **Where are you flying from and to?** 2. **What date do you need to travel?** (An
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, origin]：I'd be happy to help you book a flight, Anna. To search for options, I'll need a few details about your trip:  1. **Where are you flying from and to?** 2. **What date do you need to travel?** (and do 
- `ad-val-0014`
  - `external-longtail-multiturn-live-r1-20260902` [（没读出在问什么）]：Hello Jennifer! I'm here to help you plan your corporate travel. What trip can I arrange for you?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：Hello Jennifer! I'm here to help you plan your corporate travel.   What trip can I help you arrange? Please share the details — where you're headed, dates, and any preferences (like direct flights onl
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：Hello Jennifer! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
- `ad-val-0016`
  - `external-longtail-multiturn-live-r1-20260902` [date]：I can search for flights from SFO to JFK, but I need to know your travel date before I can look up inventory. What day do you want to fly?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：I can search for flights from SFO to JFK, but I need to know when you're traveling. What date do you want to fly, and do you need to arrive by a specific time?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can search for flights from SFO to JFK, but I need to know when you're traveling. What date do you want to fly, and do you need to arrive by a specific time?
- `ad-val-0017`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date]：What date do you need to fly from Houston (HOU) to Denver (DEN), and is there a time you need to arrive by?
  - `external-longtail-multiturn-live-r2-20260902` [date]：Which day would you like to fly from Houston (HOU) to Denver (DEN)?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that. Let me search for flights from Houston (HOU) to Denver (DEN).  Before I search, I need to know the date you want to travel. Do you have a specific date in mind, or a deadline you
- `ad-val-0018`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date]：I can help with that, but I need to know when you're traveling. Could you tell me the date you'd like to fly from ORD to SFO, and whether you need to arrive by a certain time?
  - `external-longtail-multiturn-live-r2-20260902` [date]：What date would you like to travel from ORD to SFO?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that, but I need to know when you're traveling. What date do you need to arrive in SFO, and is there a specific time you need to be there by?
- `ad-val-0019`
  - `external-longtail-multiturn-live-r1-20260902` [date, destination]：I can help with that. To search for flights, I need to know a couple of things you haven't mentioned yet:  1. **Where is the meeting?** (destination city) 2. **When do you need to be there?** (date an
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, destination]：I can help search for flights, but I need a few details to find the right options:  1. **Destination** — where is the meeting? (city/airport) 2. **Date** — when do you need to be there? (and do you ne
  - `external-longtail-multiturn-live-r3-20260902` [date, destination]：I can help search for flights, but I need a few details to find the right options:  1. **Destination** — where is the meeting? (Which city/airport?) 2. **Meeting date and time** — when do you need to 
- `ad-val-0022`
  - `external-longtail-multiturn-live-r1-20260902` [date]：I can help search for flights from JFK to IAH, but I need to know your travel date. Could you tell me when you'd like to fly?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：What date do you need to fly from JFK to IAH, and is there a time you need to arrive by (e.g., a meeting)?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that, but I need a couple of details to search for your flight:  1. **What date** do you want to fly from JFK to IAH? 2. **What time** do you need to arrive by (or is there a specific 
- `ad-val-0025`
  - `external-longtail-multiturn-live-r1-20260902` [arrive_by, date, destination, transport_mode]：I'm ready to help you plan your corporate trip, Patrick. Where are you headed, and what are the details — dates, cities, and any preferences (direct flights only, prefer train, must arrive by a certai
  - `external-longtail-multiturn-live-r2-20260902` [date, destination]：I'm ready to help you plan your corporate trip, Patrick. What's the itinerary you have in mind — where are you headed, and on what dates?
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：I'm here to help with your corporate travel. What trip can I arrange for you, Patrick?
- `ad-val-0033`
  - `external-longtail-multiturn-live-r1-20260902` [date]：What date do you need to travel from SFO to HOU, and do you have a specific arrival time you need to be there by?
  - `external-longtail-multiturn-live-r2-20260902` [date]：What date would you like to fly from SFO to HOU?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：What date do you need to fly from SFO to HOU, and do you need to arrive by a specific time (e.g., for a meeting)?
- `ad-val-0034`
  - `external-longtail-multiturn-live-r1-20260902` [date]：I can help with that, but I need to know when you're traveling. Could you tell me the date you'd like to fly from Oakland (OAK) to Washington Dulles (IAD)?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：I can help with that, but I need to know when you're traveling. What date do you need to arrive at IAD (Washington Dulles), and is there a specific time you need to be there by?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that, but I need to know when you need to travel. What date do you need to arrive at IAD (Washington Dulles), and is there a specific time you need to be there by?
- `ad-val-0036`
  - `external-longtail-multiturn-live-r1-20260902` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), not corporate travel, so I'm not able to book it. Is there a business trip I can help you arrange instead?
  - `external-longtail-multiturn-live-r2-20260902` [date, origin]：Could you tell me your departure city and travel dates? Also, is this a business trip or a personal trip with friends?
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), which is outside what I can arrange. I handle corporate travel only. Is there a business trip I can help you with inste
- `ad-val-0039`
  - `external-longtail-multiturn-live-r1-20260902` [budget, date, destination, transport_mode]：Hi Gary! I'm here to help arrange your corporate travel. What trip can I help you with? For example, where are you headed, when do you need to be there, and any preferences (direct flights, train, bud
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：Hi Gary! I'm here to help arrange your corporate travel. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260902` [date, destination]：Hi Gary! I'm here to help with your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences?
- `ad-val-0041`
  - `external-longtail-multiturn-live-r1-20260902` [（没读出在问什么）]：I'm Raymond Thompson. How can I help with your corporate travel today?
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：I'm Raymond Thompson. How can I help with your corporate travel today?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, transport_mode]：Hi Raymond, I can help arrange your corporate travel. Could you tell me where you're headed, the dates, and any preferences (e.g., direct flights only, prefer train, must arrive by a certain time)?
- `ad-val-0042`
  - `external-longtail-multiturn-live-r1-20260902` [destination]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, transport_mode]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences (like direct flights only or a preference for train vs
  - `external-longtail-multiturn-live-r3-20260902` [destination]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?

## 局限

- 第 2 层只比路线和日期，不比到达时限的具体时刻、席别过滤等更细的参数；地点按模型送出的原文比，同一地方一轮写城市名一轮写三字码（New York / JFK）会记成路线不同。
- 第 4 层是关键词规则，不一致用例里会混有漏判；精确版要 runner 把 `open_questions` 结构化落盘。
- 第 3 层（声明的硬要求）没有数据；多轮集里「办成与否不一致」的根因正在这一层。
- 一致不等于对：`consistent_but_differs_from_reference` 那一桶要单独看。

## 输入指纹

| 轮 | report.json sha256 | traces.jsonl sha256 | 搜索参数 | 轨迹 | 追问原文 |
|---|---|---|---|---|---|
| `external-longtail-multiturn-live-r1-20260902` | `3a242583a583f32d…` | `0e5721f06f61f7dd` | 无 | 有 | 有 |
| `external-longtail-multiturn-live-r2-20260902` | `a0b4a22fe697e48f…` | `4aacc578405adf22` | 有 | 有 | 有 |
| `external-longtail-multiturn-live-r3-20260902` | `613cc2fbc742f748…` | `03174478c52d4c35` | 有 | 有 | 有 |
