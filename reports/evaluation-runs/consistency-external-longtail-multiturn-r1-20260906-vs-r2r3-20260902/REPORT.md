# 三轮一致性 · 比决策不比文本 · 跨批次：09-06 r1 对 09-02 r2/r3（同一提示词、同一事实表）

- runner: `run-consistency-v1` · generated_at `2026-09-06T19:25:06.506718+00:00`
- 轮次：`external-longtail-multiturn-live-r1-20260906`、`external-longtail-multiturn-live-r2-20260902`、`external-longtail-multiturn-live-r3-20260902`
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
| 第 1 层 · 终局决策 | 175/185 = 94.6% | 三轮 `outcome` 相同的用例比例 |
| 第 2 层 · 搜索参数 | 134/143 = 93.7% | 在 3 轮里都搜了的用例中，去重后路线和日期完全相同的比例（去重前 125）；比较方式 `parameters` |
| 第 3 层 · 声明的硬要求 | 不适用 | 少于两轮带 declared_requirements（2026-09-06 之前的报告没有这个字段） |
| 第 4 层 · 追问目标 | 139/177 = 78.5% | 三轮都追问了的用例中，问的核心事实集合相同的比例（全集相同 93）；关键词粗版 |

## 第 1 层 · 终局决策

| 桶 | 数 | 含义 |
|---|---:|---|
| `consistent_and_matches_reference` | 158 | 三轮相同，且与参照一致（一致且对） |
| `consistent_but_differs_from_reference` | 9 | 三轮相同，但与参照不一致（一致但错，要人看） |
| `consistent_reference_disagrees` | 8 | 三轮决策相同，参照判断却不同（参照本身依赖轮次数据） |
| `mixed` | 10 | 三轮不同 |

各轮终局分布：

| 轮 | completed | honest_failure | out_of_scope | stuck_clarifying |
|---|---:|---:|---:|---:|
| `external-longtail-multiturn-live-r1-20260906` | 133 | 47 | 4 | 1 |
| `external-longtail-multiturn-live-r2-20260902` | 133 | 48 | 3 | 1 |
| `external-longtail-multiturn-live-r3-20260902` | 135 | 46 | 3 | 1 |

三轮不同的 10 条：

| case_id | external-longtail-multiturn-live-r1-20260906 | external-longtail-multiturn-live-r2-20260902 | external-longtail-multiturn-live-r3-20260902 |
|---|---|---|---|
| `ct-human-h20241029143455115600` | out_of_scope | out_of_scope | completed |
| `ct-human-h20241029143606240237` | honest_failure | completed | completed |
| `ct-human-h20241029143657365639` | honest_failure | completed | honest_failure |
| `ct-human-h20241029143709097610` | completed | honest_failure | completed |
| `ct-human-h20241029143722458026` | completed | honest_failure | honest_failure |
| `ct-human-h20241029143730769592` | completed | honest_failure | honest_failure |
| `ct-human-h20241029143732290071` | honest_failure | honest_failure | completed |
| `ct-human-h20241029143736524841` | honest_failure | completed | completed |
| `ad-val-0032` | completed | honest_failure | completed |
| `ad-val-0036` | out_of_scope | completed | out_of_scope |

## 第 2 层 · 搜索参数

| 桶 | 数 |
|---|---:|
| `identical` | 134 |
| `searched_in_some_compared_runs_only` | 4 |
| `dates_differ` | 3 |
| `routes_differ` | 6 |
| `search_presence_mixed`（比较的轮次里有的搜了有的没搜） | 4 |

被宿主拒掉的搜索尝试（`ToolInputError`，不算搜了）：

| 轮 | 次数 | 涉及用例 |
|---|---:|---:|
| `external-longtail-multiturn-live-r1-20260906` | 122 | 102 |
| `external-longtail-multiturn-live-r2-20260902` | 116 | 92 |
| `external-longtail-multiturn-live-r3-20260902` | 104 | 89 |

去重后仍不同的用例（每轮的签名集合）：

- `ct-human-h20241029143522848652` · `dates_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Shenzhen | Beijing | 2026-09-07
  - `external-longtail-multiturn-live-r2-20260902`: transport | Shenzhen | Beijing | 2026-09-03
  - `external-longtail-multiturn-live-r3-20260902`: transport | Shenzhen | Beijing | 2026-09-03
- `ct-human-h20241029143542560410` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Guangzhou | Nanjing | 2026-09-27; transport | Nanjing | Guangzhou | 2026-09-23
  - `external-longtail-multiturn-live-r2-20260902`: transport | Nanjing | Guangzhou | 2026-09-03
  - `external-longtail-multiturn-live-r3-20260902`: transport | Guangzhou | Nanjing | 2026-09-07; transport | Nanjing | Guangzhou | 2026-09-03
- `ct-human-h20241029143602442546` · `dates_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Guangzhou | Shenzhen | 2026-09-13; transport | Shenzhen | Guangzhou | 2026-09-12
  - `external-longtail-multiturn-live-r2-20260902`: transport | Guangzhou | Shenzhen | 2026-09-06; transport | Shenzhen | Guangzhou | 2026-09-05
  - `external-longtail-multiturn-live-r3-20260902`: transport | Guangzhou | Shenzhen | 2026-09-06; transport | Shenzhen | Guangzhou | 2026-09-05
- `ct-human-h20241029143630351554` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Chengdu | Chongqing | 2026-09-11; transport | Chongqing | Chengdu | 2026-09-13
  - `external-longtail-multiturn-live-r2-20260902`: transport | Chengdu | Chongqing | 2026-09-23; transport | Chongqing | Chengdu | 2026-09-24
  - `external-longtail-multiturn-live-r3-20260902`: hotel | Chongqing | 2026-09-05 | 2026-09-07; transport | Chengdu | Chongqing | 2026-09-05
- `ct-human-h20241029143639901784` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Shanghai | Guangzhou | 2026-09-07; transport | Shanghai | Guangzhou | 2026-09-08; transport | Shanghai | Guangzhou | 2026-09-09; transport | Shanghai | Guangzhou | 2026-09-10; transport | Shanghai | Guangzhou | 2026-09-11; transport | Shanghai | Guangzhou | 2026-09-12; transport | Shanghai | Guangzhou | 2026-09-13
  - `external-longtail-multiturn-live-r2-20260902`: transport | Guangzhou | Shanghai | 2026-09-08; transport | Shanghai | Guangzhou | 2026-09-06
  - `external-longtail-multiturn-live-r3-20260902`: transport | Guangzhou | Shanghai | 2026-09-08; transport | Shanghai | Guangzhou | 2026-09-06
- `ct-human-h20241029143818664124` · `dates_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Chongqing | Wuhan | 2026-09-12; transport | Wuhan | Chongqing | 2026-09-13
  - `external-longtail-multiturn-live-r2-20260902`: transport | Chongqing | Wuhan | 2026-09-05; transport | Wuhan | Chongqing | 2026-09-06
  - `external-longtail-multiturn-live-r3-20260902`: transport | Chongqing | Wuhan | 2026-09-05; transport | Wuhan | Chongqing | 2026-09-06
- `ad-val-0007` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | LGA | PHX | 2027-02-28; transport | PHX | LGA | 2027-02-26
  - `external-longtail-multiturn-live-r2-20260902`: transport | EWR | PHX | 2027-02-28; transport | JFK | PHX | 2027-02-28; transport | LGA | PHX | 2027-02-28; transport | PHX | EWR | 2027-02-26; transport | PHX | JFK | 2027-02-26; transport | PHX | LGA | 2027-02-26
  - `external-longtail-multiturn-live-r3-20260902`: transport | LGA | PHX | 2027-02-28; transport | PHX | LGA | 2027-02-26
- `ad-val-0019` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Las Vegas | Phoenix | 2027-02-04; transport | Phoenix | Las Vegas | 2027-02-06
  - `external-longtail-multiturn-live-r2-20260902`: transport | LAS | PHX | 2027-02-04; transport | PHX | LAS | 2027-02-06
  - `external-longtail-multiturn-live-r3-20260902`: transport | LAS | PHX | 2027-02-04; transport | PHX | LAS | 2027-02-06
- `ad-val-0031` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Denver | New York | 2027-03-22; transport | New York | Denver | 2027-03-20
  - `external-longtail-multiturn-live-r2-20260902`: transport | Denver | New York | 2027-03-22; transport | New York | Denver | 2027-03-20
  - `external-longtail-multiturn-live-r3-20260902`: transport | DEN | JFK | 2027-03-22; transport | JFK | DEN | 2027-03-20

## 第 3 层 · 交付时声明的硬要求与偏好

少于两轮带 declared_requirements（2026-09-06 之前的报告没有这个字段）

## 第 4 层 · 追问目标（粗版）

方法：`keyword-rules-v1`。核心事实（date, origin, destination, arrive_by）在整段话里出现即算问了；附带事实只在带问号或「吗 / 还是 / 需不需要」的句子里才算。

三轮都追问了的用例 177；核心事实集合一致 139；全集一致 93；只有附带事实不同 46；有的轮追问有的轮没有 3（与第 1 层的不一致重叠）。

第一轮终局动作：这批报告没有 `loop` 记录，未比较。

| 问的事 | 至少一轮问了 | 三轮一致 | 一致率 |
|---|---:|---:|---:|
| `date` | 168 | 162 | 91.5% |
| `origin` | 12 | 168 | 94.9% |
| `destination` | 20 | 163 | 92.1% |
| `arrive_by` | 19 | 160 | 90.4% |
| `lodging` | 55 | 130 | 73.5% |
| `transport_mode` | 34 | 144 | 81.4% |
| `travelers` | 0 | 177 | 100.0% |
| `budget` | 7 | 170 | 96.0% |
| `intent_confirm` | 11 | 167 | 94.3% |

核心事实不同的 38 条（需人看是真差异还是关键词漏判）：

- `ct-human-h20241029143526214076`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination]：我来帮你规划这趟北京到苏州的3天旅行。不过先说明一下：我这边能帮你查的是**交通和酒店**的实时库存，景点和美食推荐我可以结合常识给你建议，但具体行程安排（去哪些景点、吃什么）需要你自己定。  先帮你查一下北京到苏州的交通。不过我需要确认一个关键信息：**你们打算哪天出发？** 这决定了我能查哪天的票。  在等你回复的同时，我先说明一下苏州值得去的景点和美食，供你参考：  **著名景点（3天可以这
  - `external-longtail-multiturn-live-r2-20260902` [date]：你们打算哪天从北京出发去苏州？3天行程的话，出发日期定了我就能帮你查车票/机票。
  - `external-longtail-multiturn-live-r3-20260902` [date]：你们计划哪天从北京出发去苏州呢？（比如这周五、下周一，或者具体日期）我好按日期帮你们查车票/机票。
- `ct-human-h20241029143530430866`
  - `external-longtail-multiturn-live-r1-20260906` [date]：带父母去北京玩，我想先确认一下出发日期：您打算哪天从深圳出发、哪天返回呢？（比如"9月10日出发，玩5天"）
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, lodging, origin, transport_mode]：我来帮您规划这趟深圳到北京的亲子游。先确认一下行程安排：  - **出发地**：深圳 - **目的地**：北京 - **人数**：3人（您和父母） - **天数**：5天  不过有几个关键信息我还需要向您确认，才能开始搜索：  1. **出发日期**：您计划哪天出发？（比如"9月10日出发"） 2. **交通偏好**：深圳到北京距离较远，您倾向坐飞机还是高铁？考虑到老人旅途不要太辛苦，飞机可能更省
  - `external-longtail-multiturn-live-r3-20260902` [date]：您还没有告诉我出发日期。请问您计划哪天从深圳出发去北京？（旅行天数5天，我可以据此帮您安排往返和行程节奏。）
- `ct-human-h20241029143532210869`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你打算哪天出发去北京？5天行程的话，回程日期也请一并告诉我（比如 9月10日出发、9月14日回）。
  - `external-longtail-multiturn-live-r2-20260902` [date, origin]：你打算哪天出发去北京？5天行程是从哪天开始算？（比如"9月5日出发，玩到9月9日"）
  - `external-longtail-multiturn-live-r3-20260902` [date, origin]：你打算哪天出发去北京？5天行程是从哪天开始算？（比如「9月5日出发」或「这周五出发」都行，我好按这个日期查车票/机票）
- `ct-human-h20241029143533982475`
  - `external-longtail-multiturn-live-r1-20260906` [date]：您这趟是去巴塞罗那，中间在北京大兴机场附近停留三天。请问这三天具体是哪几天（比如几号到几号）？另外，您需要我帮您安排成都→北京、北京→巴塞罗那的交通，还是只想要北京大兴机场附近免费景点的推荐？
  - `external-longtail-multiturn-live-r2-20260902` [date, intent_confirm]：你这次主要是想让我帮你安排成都→北京→巴塞罗那的交通（含北京大兴机场附近三天的停留），还是只想要北京大兴机场附近免费景点的推荐？另外，北京停留的三天具体是哪几天（比如几号到几号）？
  - `external-longtail-multiturn-live-r3-20260902` [lodging]：抱歉，我需要先说明一下：您这条请求主要是想了解北京大兴机场附近有哪些**免费且交通便利的景点**，这属于旅游景点推荐，而不是我可以帮您安排的差旅预订（机票、酒店、火车票等）。  不过我可以直接告诉您一些信息，同时说明我的边界：  **大兴机场附近免费景点（供参考，非预订服务）：**  1. **大兴机场本身** — 机场航站楼本身就是个"景点"，有"凤凰展翅"造型、中轴线景观、免税店和艺术装置，免
- `ct-human-h20241029143542560410`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, lodging]：明天是 2026-09-07。您要去广州玩 5 天，但您问的是"好玩的地方"——这是旅游景点推荐，不是差旅安排（订交通、订酒店）。  我这边能帮您做的是：查南京到广州的交通、以及广州的酒店。景点游玩攻略不在我的能力范围内。  不过我可以先帮您把交通和住宿查好。请问：  1. 您需要我帮您订**南京→广州**的交通吗？如果需要，您希望哪天出发、大概几点到？ 2. 需要帮您订**广州的酒店**吗（5 
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging]：回程日期：5天行程，是9月7日还是9月8日回南京？需要我帮你查返程票吗？ 住宿：需要我帮你订广州的酒店吗（9月3日入住，住几晚）？
  - `external-longtail-multiturn-live-r3-20260902` [lodging]：返程想坐哪个时刻的航班？ 是否需要我帮你订广州的酒店？
- `ct-human-h20241029143543943871`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, lodging, origin, transport_mode]：我先确认一下这趟行程的关键信息，再帮你查交通和住宿。  目前我理解到的： - **出发地**：成都 - **目的地**：北京 - **人数**：4人 - **时长**：5天左右 - **兴趣**：名校氛围（清华、北大等）+ 历史文化（故宫、长城、胡同等）  在动手查票之前，有几个日期信息我还没法从你的话里确定，需要你告诉我：  1. **出发日期**：你们打算哪天从成都出发？（比如 9月10日、下
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging, transport_mode]：我先确认一下：这是一次从成都到北京的4人5天游玩行程，属于差旅安排，我可以帮你查交通和住宿。  不过有几个关键信息需要你补充，我才能开始搜索：  1. **出发日期**：你们打算哪天从成都出发？（5天行程，我需要知道具体日期才能查票） 2. **住宿**：需要我帮忙订北京的酒店吗？（4人，5天） 3. **交通偏好**：成都到北京，你们倾向坐高铁还是飞机？有没有"必须直飞"或"必须高铁"这类硬性要
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你们计划哪天从成都出发去北京？（比如「9月10号出发」或「下周三出发」都可以）
- `ct-human-h20241029143602442546`
  - `external-longtail-multiturn-live-r1-20260906` [lodging]：是否需要我帮你订广州的酒店（2人、住1晚）？如需，请告诉我偏好的区域或价位。
  - `external-longtail-multiturn-live-r2-20260902` [date, transport_mode]：回程（9月6日周日 广州→深圳）航班无库存，是否改坐高铁/大巴，或由你们自行安排回程？ Guangzhou→Shenzhen 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。
  - `external-longtail-multiturn-live-r3-20260902` [date]：回程广州→深圳在9月6日（周日）没有查到可订的航班/车次，是否改到周一（9月7日）返回，或回程自行安排？ Guangzhou→Shenzhen 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。
- `ct-human-h20241029143604280402`
  - `external-longtail-multiturn-live-r1-20260906` [date]：您说的"端午假期三天"具体是哪三天？2026年端午节是6月19日（周五），通常假期是6月19日—6月21日。是这个日期吗？还是您有别的安排？
  - `external-longtail-multiturn-live-r2-20260902` [date]：您说的"端午假期三天"具体是哪三天？2026年端午节是6月19日（周五），假期通常是6月19日-21日。您希望哪天从杭州出发去上海，第三天（6月21日）晚上19点前回到杭州，对吗？
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：端午假期三天具体是哪三天出发？2026年端午节是6月19日（周五），三天假期通常是6月19日—6月21日。请问您是6月19日从杭州出发去上海，第三天（6月21日）晚上19点前返回杭州吗？
- `ct-human-h20241029143625307702`
  - `external-longtail-multiturn-live-r1-20260906` [date]：您计划哪天从北京出发去上海？（停留3天，请告知出发日期，我好帮您查往返交通票）
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, origin]：您具体哪天从北京出发去上海？停留3天是从哪天到哪天？告诉我出发日期，我就能帮您搜北京→上海的交通（高铁/航班）。
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你计划哪天从北京出发去上海？（停留3天，我需要出发日期才能查询交通和酒店）
- `ct-human-h20241029143628653733`
  - `external-longtail-multiturn-live-r1-20260906` [date, origin]：您出发地是北京还是南京？（正文写"从北京出发"，但位置信息显示南京）。另外，请告诉我出发日期和返回日期，我好帮您查车票/机票。
  - `external-longtail-multiturn-live-r2-20260902` [date]：出发城市是北京还是南京？（你开头写"当前位置南京"，正文又写"从北京出发"，两者不一致）另外，3天行程具体哪天出发？
  - `external-longtail-multiturn-live-r3-20260902` [date, origin]：你的出发地是北京还是南京？（正文写"从北京出发"，但位置信息显示"当前位置南京"）另外，你计划哪天出发去上海？
- `ct-human-h20241029143630351554`
  - `external-longtail-multiturn-live-r1-20260906` [lodging]：住宿是否需要我帮你订酒店（还是你们自己安排）？ 两天行程想侧重哪类：市区历史文化（磁器口/洪崖洞/三峡博物馆）还是凉快风景（南山/武隆仙女山）？
  - `external-longtail-multiturn-live-r2-20260902` [date, lodging]：我先确认一下：这是一次休闲旅行规划（成都→重庆，2人，2天），不是差旅订票。你希望我帮你安排交通和住宿吗？还是只需要行程建议（吃什么、玩什么）？  不过我可以先帮你查一下成都到重庆的交通选项，方便你参考。请问你打算哪天出发、哪天返回？
  - `external-longtail-multiturn-live-r3-20260902` [date]：返程日期和大致时间还没定：是周日（9/7）当天回成都，还是周一（9/8）再回？定了我帮你搜返程票。
- `ct-human-h20241029143632058747`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：您提到在北京转机、想安排一天行程，但有几个关键信息我还没法确定：1）转机的具体日期是哪天（几号到北京、几号离开）？2）是否需要我帮您安排重庆到北京、以及北京到下一站的机票/交通？3）住宿是否需要安排（还是当天转机不过夜）？
  - `external-longtail-multiturn-live-r2-20260902` [date, destination]：您这次是转机路过北京，想安排一天的城市游览。请问您需要我帮忙安排的是：1）重庆到北京的交通（以及后续转机离开北京的交通），还是 2）只帮您规划北京一天值得去的景点行程？另外，您转机去往的目的地是哪里、大概哪天到北京？
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, lodging, transport_mode]：您提到在北京转机、想安排一天行程探索景点，但这条请求里没有给出任何可搜索的差旅要素（出发/到达日期、航班或高铁班次、是否需要订酒店）。请问您需要我帮您安排的是：① 重庆—北京的交通（哪天出发、几点前要到北京）？还是 ② 北京当地的酒店？景点游览本身不在我的差旅搜索范围内，我可以帮您查交通和住宿，但具体"最值得去的地方"需要您自己定或另找攻略。
- `ct-human-h20241029143706437033`
  - `external-longtail-multiturn-live-r1-20260906` [date]：您想哪天出发去重庆呢？（四天行程，往返都坐高铁）告诉我出发日期，我就能帮您查好往返高铁票并规划这四天的行程。
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, origin]：您还没有告诉我具体的出发日期，所以我暂时无法搜索高铁票。不过我可以先把行程框架和预算帮您理清楚，等您告诉我出发日期，我立刻去查往返高铁票。  先说明一下我目前掌握的信息和需要您确认的一点：  **已明确的：** - 出发地：南京 → 目的地：重庆 - 人数：2 人 - 天数：4 天 - 交通：必须高铁（G 字头）往返 - 想去的景点：三峡博物馆、重庆动物园、朝天门、人民大礼堂 - 想体验重庆本地饮
  - `external-longtail-multiturn-live-r3-20260902` [date]：您想去重庆玩四天，但还没有告诉我具体是哪一天出发。高铁往返的日期需要您先定下来，我才能帮您查票。  请问您计划**哪一天从南京出发**去重庆？（比如 9月10日出发、9月13日返回，或者您方便的具体日期）
- `ct-human-h20241029143832205713`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：您说的"中秋节"出发，是指 2026 年 9 月 25 日（周五，农历八月十五）当天从杭州飞广州吗？还是想提前一两天出发、在中秋假期里玩满五天？
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：您说的"中秋节"出发，是指中秋节当天（2026年9月25日，周五）出发，还是想在中秋假期（9月25日-27日）的某一天出发？另外，两个孩子想去的海洋馆和动物园，您希望我帮您查一下广州有哪些推荐吗？
  - `external-longtail-multiturn-live-r3-20260902` [date]：您说的"中秋节"具体是哪一天出发？2026年中秋节是9月25日（周五），中秋假期通常是9月25日-27日。您希望哪天从杭州飞广州？
- `ct-human-h20241029143834377206`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：请问您计划哪天出发去南京？当天有需要几点前到达的安排吗（比如会议时间）？
  - `external-longtail-multiturn-live-r2-20260902` [date]：我来帮您安排广州到南京的行程。不过我需要确认一下出发日期——您提到旅行天数1天，但没有说明具体哪一天出发。  让我先确认一下：您计划哪天出发去南京？
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问你们计划哪天出发去南京？需要哪天到达（比如某天上午前要到）？告诉我具体日期，我就能帮你们查广州到南京的交通。
- `ct-human-h20241029143840382242`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging, transport_mode]：我先确认一下这趟行程的关键信息，然后帮你查交通。  你提到"3个人去北京玩5天"，但还没有说**出发日期**和**返程日期**。这是规划旅程必须的信息。  另外，关于"体验历史文化"，我可以帮你梳理北京5天的历史文化行程建议（故宫、长城、天坛、颐和园、胡同等），但**交通和住宿的预订**需要你告诉我具体日期。  请问：  1. **出发日期**是哪天？（比如"9月10日出发"） 2. 需要我帮你*
  - `external-longtail-multiturn-live-r2-20260902` [date, origin]：您计划哪天从南京出发去北京？5天行程大概想从哪天开始（比如9月几号出发）？
  - `external-longtail-multiturn-live-r3-20260902` [date, lodging]：我来帮你规划这趟南京→北京的5天历史文化之旅。先确认一下：你提到"旅行天数5天"，但没有说具体出发日期。我需要知道出发日期才能搜索交通和住宿。  另外，关于住宿——你是希望我帮你安排酒店，还是只规划交通和行程？  先告诉我出发日期吧，这样我就能开始搜索了。
- `ct-human-h20241029143846901147`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, lodging, transport_mode]：我先确认一下这趟行程的关键信息，然后帮你查交通和住宿。  不过有几个点需要先跟你对齐：  1. **出发日期**：你打算哪天从南京出发去北京？（5天行程，我需要知道具体日期才能查票） 2. **住宿**：5天4晚，需要我帮你安排北京的酒店吗？还是你自己订？ 3. **交通方式**：南京到北京，你倾向高铁还是飞机？有没有时间上的硬要求（比如必须几点前到）？  你先把出发日期告诉我，我就能开始查票了。
  - `external-longtail-multiturn-live-r2-20260902` [date]：我需要先确认出发日期。您说"去北京5天"，但没有说明具体哪天出发。请问您计划哪天出发去北京？
  - `external-longtail-multiturn-live-r3-20260902` [date, lodging]：这趟北京5天行程，您希望哪天出发、哪天返回？另外住宿需要我帮您一起安排吗（还是您自己订）？
- `ct-human-h20241029143900796443`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, origin, transport_mode]：您还没有告诉我具体的出发日期，所以我暂时无法查到高铁班次。不过我可以先把行程框架和需要您确认的信息整理好。  **已明确的行程信息：** - 出发地：北京 → 目的地：苏州 - 人数：2 人（您和女朋友） - 天数：4 天 - 交通：往返都坐高铁（硬性要求） - 预算：尽量低（偏好）  **需要您补充的信息：** 1. **出发日期**：您打算哪天从北京出发？（例如"9月10日出发"或"下周三出发
  - `external-longtail-multiturn-live-r2-20260902` [date]：请问您计划哪天从北京出发去苏州？（玩四天，往返都坐高铁，我会按预算尽量低来筛选）
  - `external-longtail-multiturn-live-r3-20260902` [date]：请问您计划哪天从北京出发去苏州？（玩四天，往返都坐高铁，我会按这个安排）
- `ct-human-h20241029143906048411`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你们打算哪天出发去上海呢？（比如这周五、下周一，或者具体日期）定下出发日期我就能帮你们查南京到上海的车票/机票了。
  - `external-longtail-multiturn-live-r2-20260902` [date]：请问您计划哪天出发去上海？（例如：9月5日出发，玩3天）
  - `external-longtail-multiturn-live-r3-20260902` [budget, date, destination, lodging]：我先确认一下：您是从南京出发去上海玩3天，3个人，预算5000以内，偏好现代繁华的地方。不过您还没有告诉我出发日期和返回日期，这决定了我能查到哪些实际的车票/机票。  在您补充日期之前，我先说明一下我能帮您做的：  1. **交通**：南京→上海（去程）、上海→南京（返程），我可以查高铁/航班的实际班次和价格。 2. **住宿**：您提到"玩3天"，如果需要我帮您订酒店，请告诉我；如果只是行程建议
- `ct-human-h20241029143913181191`
  - `external-longtail-multiturn-live-r1-20260906` [lodging]：这趟是问南京的景点推荐，不是要我帮你订车票/酒店。请问你需要我帮你安排苏州到南京的往返交通（或住宿）吗？还是只想听景点推荐？
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, intent_confirm, lodging]：这趟是问南京的景点推荐，不是要我帮你订车票/酒店，对吧？如果是想让我安排苏州到南京的往返交通和住宿，告诉我你们打算哪个周末出发、大概几点到南京，我就能帮你查票。
  - `external-longtail-multiturn-live-r3-20260902` [transport_mode]：这趟是问南京的景点推荐，不是要我帮你订车票或酒店。请问你需要我帮你查苏州到南京的交通（高铁/火车）吗？还是只想先了解南京有哪些值得逛的江南水乡景点？
- `ad-val-0002`
  - `external-longtail-multiturn-live-r1-20260906` [date]：What date do you need to be in New York for the wedding?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：What date is the wedding in New York? (And if you need to arrive by a certain time that day, please let me know.)
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I'll search for flights from Denver (DEN) to New York LaGuardia (LGA). But first, I need to know the date of the wedding — that's the one thing I can't derive from your message.  Could you tell me the
- `ad-val-0004`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：What date does the pioneer festival take place, and do you need to arrive by a specific time that day?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：What date does the pioneer festival take place (and do you need to arrive by a specific time that day)?
  - `external-longtail-multiturn-live-r3-20260902` [date]：What date does the pioneer festival take place (or what date do you need to arrive in Detroit by)?
- `ad-val-0007`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, transport_mode]：Hello David! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for tr
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：Hello David! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260902` [date, destination, transport_mode]：Hello David! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for t
- `ad-val-0008`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：Hello Gary! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
  - `external-longtail-multiturn-live-r2-20260902` [destination]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r3-20260902` [destination]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
- `ad-val-0010`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, origin]：I'd be happy to help you book a flight, Anna. To search for real options, I need to know a few details about your trip:  1. **Where are you flying from and to?** (origin and destination cities) 2. **W
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, origin]：I'd be happy to help you book a flight, Anna. To search for options, I need to know a few details about your trip:  1. **Where are you flying from and to?** 2. **What date do you need to travel?** (An
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, origin]：I'd be happy to help you book a flight, Anna. To search for options, I'll need a few details about your trip:  1. **Where are you flying from and to?** 2. **What date do you need to travel?** (and do 
- `ad-val-0014`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：Hello Jennifer! I'm here to help you plan your corporate travel. What trip can I help you arrange?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：Hello Jennifer! I'm here to help you plan your corporate travel.   What trip can I help you arrange? Please share the details — where you're headed, dates, and any preferences (like direct flights onl
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：Hello Jennifer! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
- `ad-val-0016`
  - `external-longtail-multiturn-live-r1-20260906` [date]：I can search for flights from SFO to JFK, but I need to know your travel date before I can look up inventory. Could you tell me which day you'd like to fly?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：I can search for flights from SFO to JFK, but I need to know when you're traveling. What date do you want to fly, and do you need to arrive by a specific time?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can search for flights from SFO to JFK, but I need to know when you're traveling. What date do you want to fly, and do you need to arrive by a specific time?
- `ad-val-0017`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：I can help with that, but I need a couple of details to search real inventory:  1. **Travel date** — when do you want to fly? (And do you need to arrive by a certain time that day?) 2. **Return** — is
  - `external-longtail-multiturn-live-r2-20260902` [date]：Which day would you like to fly from Houston (HOU) to Denver (DEN)?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that. Let me search for flights from Houston (HOU) to Denver (DEN).  Before I search, I need to know the date you want to travel. Do you have a specific date in mind, or a deadline you
- `ad-val-0018`
  - `external-longtail-multiturn-live-r1-20260906` [date]：What date would you like to travel from ORD to SFO?
  - `external-longtail-multiturn-live-r2-20260902` [date]：What date would you like to travel from ORD to SFO?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that, but I need to know when you're traveling. What date do you need to arrive in SFO, and is there a specific time you need to be there by?
- `ad-val-0019`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination]：I can help with that, but I need to know where the meeting is and when you need to be there. Could you tell me:  1. **Destination city** — where is the meeting? 2. **Date and time** — when do you need
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date, destination]：I can help search for flights, but I need a few details to find the right options:  1. **Destination** — where is the meeting? (city/airport) 2. **Date** — when do you need to be there? (and do you ne
  - `external-longtail-multiturn-live-r3-20260902` [date, destination]：I can help search for flights, but I need a few details to find the right options:  1. **Destination** — where is the meeting? (Which city/airport?) 2. **Meeting date and time** — when do you need to 
- `ad-val-0025`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：I'm here to help with your corporate travel. What trip can I arrange for you?
  - `external-longtail-multiturn-live-r2-20260902` [date, destination]：I'm ready to help you plan your corporate trip, Patrick. What's the itinerary you have in mind — where are you headed, and on what dates?
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：I'm here to help with your corporate travel. What trip can I arrange for you, Patrick?
- `ad-val-0033`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：What date do you need to travel from SFO to HOU, and is there a time you need to arrive by (e.g., a meeting)?
  - `external-longtail-multiturn-live-r2-20260902` [date]：What date would you like to fly from SFO to HOU?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：What date do you need to fly from SFO to HOU, and do you need to arrive by a specific time (e.g., for a meeting)?
- `ad-val-0034`
  - `external-longtail-multiturn-live-r1-20260906` [date]：What date do you want to fly from OAK to IAD?
  - `external-longtail-multiturn-live-r2-20260902` [arrive_by, date]：I can help with that, but I need to know when you're traveling. What date do you need to arrive at IAD (Washington Dulles), and is there a specific time you need to be there by?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date]：I can help with that, but I need to know when you need to travel. What date do you need to arrive at IAD (Washington Dulles), and is there a specific time you need to be there by?
- `ad-val-0036`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), not corporate travel. I can only help arrange business travel. Is there a work-related trip I can help you with instead
  - `external-longtail-multiturn-live-r2-20260902` [date, origin]：Could you tell me your departure city and travel dates? Also, is this a business trip or a personal trip with friends?
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), which is outside what I can arrange. I handle corporate travel only. Is there a business trip I can help you with inste
- `ad-val-0037`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, destination, transport_mode]：I'm Linda Smith — nice to meet you. I can help you plan corporate travel: flights, trains, and hotels for your business trips.  What trip can I help you arrange? For example, where are you headed, on 
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：I'm Linda's travel assistant. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260902` [（没读出在问什么）]：I'm Linda Smith — nice to meet you. I'm your corporate travel assistant. What trip can I help you arrange?
- `ad-val-0039`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, transport_mode]：Hi Gary! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, cities, and any preferences (like direct flights only, or a preference for train vs. fl
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：Hi Gary! I'm here to help arrange your corporate travel. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260902` [date, destination]：Hi Gary! I'm here to help with your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences?
- `ad-val-0041`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：I'm Raymond's travel assistant. What trip can I help you arrange?
  - `external-longtail-multiturn-live-r2-20260902` [（没读出在问什么）]：I'm Raymond Thompson. How can I help with your corporate travel today?
  - `external-longtail-multiturn-live-r3-20260902` [arrive_by, date, transport_mode]：Hi Raymond, I can help arrange your corporate travel. Could you tell me where you're headed, the dates, and any preferences (e.g., direct flights only, prefer train, must arrive by a certain time)?
- `ad-val-0042`
  - `external-longtail-multiturn-live-r1-20260906` [destination]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r2-20260902` [date, destination, transport_mode]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences (like direct flights only or a preference for train vs
  - `external-longtail-multiturn-live-r3-20260902` [destination]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?

## 过程（不设门禁）· 同一条用例三轮各花了多少步

次数不是决策，按离散度（三轮最大值减最小值）报；超过阈值的用例列出来给人看。

| 指标 | 用例 | 三轮相同 | 离散度中位数 | 最大 | ≥ 阈值 | 各轮合计 |
|---|---:|---:|---:|---:|---:|---|
| 模型调用次数 | 185 | 91 | 1 | 4 | 1（阈值 4） | 632 / 625 / 609 |
| 成功搜索次数 | 185 | 166 | 0 | 5 | 10（阈值 2） | 318 / 311 / 313 |
| 被宿主拒掉的搜索次数 | — | — | — | — | — | 没有数据 |
| 对话轮数 | 185 | 180 | 0 | 1 | 5（阈值 1） | 358 / 359 / 357 |

模型调用次数离散度最大的用例（值按轮次顺序）：

- `ct-human-h20241029143639901784`：[7, 3, 3]（离散度 4）

## 局限

- 第 2 层只比路线和日期，不比到达时限的具体时刻、席别过滤等更细的参数；地点按模型送出的原文比，同一地方一轮写城市名一轮写三字码（New York / JFK）会记成路线不同。
- 第 4 层是关键词规则，不一致用例里会混有漏判；精确版要 runner 把 `open_questions` 结构化落盘。
- 第 3 层要报告带 `declared_requirements`（2026-09-06 起的 runner）；多轮集里「办成与否不一致」的根因正在这一层。偏好没有证据字段，模型在推断而不是引用，所以只报告。
- 四层跟着工具表走，工具表变了层要重定；过程指标只报离散度，不判相同。
- 一致不等于对：`consistent_but_differs_from_reference` 那一桶要单独看。

## 输入指纹

| 轮 | report.json sha256 | traces.jsonl sha256 | 搜索参数 | 轨迹 | 追问原文 |
|---|---|---|---|---|---|
| `external-longtail-multiturn-live-r1-20260906` | `7298ee3183c5fd24…` | `0d8ab991a25f74b9` | 有 | 有 | 有 |
| `external-longtail-multiturn-live-r2-20260902` | `a0b4a22fe697e48f…` | `4aacc578405adf22` | 有 | 有 | 有 |
| `external-longtail-multiturn-live-r3-20260902` | `613cc2fbc742f748…` | `03174478c52d4c35` | 有 | 有 | 有 |
