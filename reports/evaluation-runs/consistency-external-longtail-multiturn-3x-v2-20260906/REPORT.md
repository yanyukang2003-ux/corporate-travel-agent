# 三轮一致性 · 比决策不比文本 · D19 多轮 185 条 × 3 轮（2026-09-06，带第 3 层）

- runner: `run-consistency-v1` · generated_at `2026-09-06T19:25:06.405050+00:00`
- 轮次：`external-longtail-multiturn-live-r1-20260906`、`external-longtail-multiturn-live-r2-20260906`、`external-longtail-multiturn-live-r3-20260906`
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
| 第 1 层 · 终局决策 | 174/185 = 94.0% | 三轮 `outcome` 相同的用例比例 |
| 第 2 层 · 搜索参数 | 138/143 = 96.5% | 在 3 轮里都搜了的用例中，去重后路线和日期完全相同的比例（去重前 128）；比较方式 `parameters` |
| 第 3 层 · 声明的硬要求 | 129/137 = 94.2% | 三轮都交付了的用例中，声明的硬要求集合相同的比例；硬要求改变可行性，设门禁 |
| 第 3 层 · 声明的偏好（只报告） | 85/137 = 62.0% | 偏好集合相同的比例；偏好只改排序，不设门禁（硬要求和偏好都相同 82） |
| 第 4 层 · 追问目标 | 136/179 = 76.0% | 三轮都追问了的用例中，问的核心事实集合相同的比例（全集相同 101）；关键词粗版 |

## 第 1 层 · 终局决策

| 桶 | 数 | 含义 |
|---|---:|---|
| `consistent_and_matches_reference` | 163 | 三轮相同，且与参照一致（一致且对） |
| `consistent_but_differs_from_reference` | 11 | 三轮相同，但与参照不一致（一致但错，要人看） |
| `mixed` | 11 | 三轮不同 |

各轮终局分布：

| 轮 | completed | honest_failure | out_of_scope | stuck_clarifying |
|---|---:|---:|---:|---:|
| `external-longtail-multiturn-live-r1-20260906` | 133 | 47 | 4 | 1 |
| `external-longtail-multiturn-live-r2-20260906` | 133 | 46 | 6 | 0 |
| `external-longtail-multiturn-live-r3-20260906` | 133 | 44 | 7 | 1 |

三轮不同的 11 条：

| case_id | external-longtail-multiturn-live-r1-20260906 | external-longtail-multiturn-live-r2-20260906 | external-longtail-multiturn-live-r3-20260906 |
|---|---|---|---|
| `ct-human-h20241029143508251643` | completed | out_of_scope | out_of_scope |
| `ct-human-h20241029143538018046` | completed | out_of_scope | out_of_scope |
| `ct-human-h20241029143606240237` | honest_failure | completed | completed |
| `ct-human-h20241029143704161138` | honest_failure | honest_failure | completed |
| `ct-human-h20241029143709097610` | completed | honest_failure | honest_failure |
| `ct-human-h20241029143722458026` | completed | honest_failure | completed |
| `ct-human-h20241029143736524841` | honest_failure | honest_failure | completed |
| `ct-human-h20241029143818664124` | honest_failure | completed | honest_failure |
| `ct-human-h20241029143913181191` | honest_failure | honest_failure | out_of_scope |
| `ad-val-0015` | honest_failure | completed | honest_failure |
| `ad-val-0030` | stuck_clarifying | completed | stuck_clarifying |

## 第 2 层 · 搜索参数

| 桶 | 数 |
|---|---:|
| `identical` | 138 |
| `searched_in_some_compared_runs_only` | 3 |
| `dates_differ` | 2 |
| `routes_differ` | 3 |
| `search_presence_mixed`（比较的轮次里有的搜了有的没搜） | 3 |

被宿主拒掉的搜索尝试（`ToolInputError`，不算搜了）：

| 轮 | 次数 | 涉及用例 |
|---|---:|---:|
| `external-longtail-multiturn-live-r1-20260906` | 122 | 102 |
| `external-longtail-multiturn-live-r2-20260906` | 119 | 96 |
| `external-longtail-multiturn-live-r3-20260906` | 113 | 94 |

去重后仍不同的用例（每轮的签名集合）：

- `ct-human-h20241029143630351554` · `dates_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Chengdu | Chongqing | 2026-09-11; transport | Chongqing | Chengdu | 2026-09-13
  - `external-longtail-multiturn-live-r2-20260906`: transport | Chengdu | Chongqing | 2026-09-11; transport | Chongqing | Chengdu | 2026-09-13
  - `external-longtail-multiturn-live-r3-20260906`: transport | Chengdu | Chongqing | 2026-09-12; transport | Chongqing | Chengdu | 2026-09-13
- `ct-human-h20241029143639901784` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Shanghai | Guangzhou | 2026-09-07; transport | Shanghai | Guangzhou | 2026-09-08; transport | Shanghai | Guangzhou | 2026-09-09; transport | Shanghai | Guangzhou | 2026-09-10; transport | Shanghai | Guangzhou | 2026-09-11; transport | Shanghai | Guangzhou | 2026-09-12; transport | Shanghai | Guangzhou | 2026-09-13
  - `external-longtail-multiturn-live-r2-20260906`: transport | Guangzhou | Shanghai | 2026-09-15; transport | Shanghai | Guangzhou | 2026-09-13
  - `external-longtail-multiturn-live-r3-20260906`: transport | Guangzhou | Shanghai | 2026-09-15; transport | Shanghai | Guangzhou | 2026-09-13
- `ct-human-h20241029143730769592` · `dates_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Hangzhou | Nanjing | 2026-09-07; transport | Hangzhou | Nanjing | 2026-09-08; transport | Nanjing | Hangzhou | 2026-09-07
  - `external-longtail-multiturn-live-r2-20260906`: transport | Hangzhou | Nanjing | 2026-09-07; transport | Hangzhou | Nanjing | 2026-09-08; transport | Nanjing | Hangzhou | 2026-09-07
  - `external-longtail-multiturn-live-r3-20260906`: transport | Hangzhou | Nanjing | 2026-09-07; transport | Nanjing | Hangzhou | 2026-09-07
- `ad-val-0019` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | Las Vegas | Phoenix | 2027-02-04; transport | Phoenix | Las Vegas | 2027-02-06
  - `external-longtail-multiturn-live-r2-20260906`: transport | Las Vegas | Phoenix | 2027-02-04; transport | Phoenix | Las Vegas | 2027-02-06
  - `external-longtail-multiturn-live-r3-20260906`: transport | LAS | PHX | 2027-02-04; transport | PHX | LAS | 2027-02-06
- `ad-val-0032` · `routes_differ`
  - `external-longtail-multiturn-live-r1-20260906`: transport | JFK | LAS | 2027-08-08; transport | LAS | JFK | 2027-08-06
  - `external-longtail-multiturn-live-r2-20260906`: transport | Las Vegas | New York | 2027-08-06; transport | New York | Las Vegas | 2027-08-08
  - `external-longtail-multiturn-live-r3-20260906`: transport | Las Vegas | New York JFK | 2027-08-06; transport | New York JFK | Las Vegas | 2027-08-08

## 第 3 层 · 交付时声明的硬要求与偏好

三轮都交付了的用例 137；硬要求相同 129；偏好相同 85；两者都相同 82。

| 桶 | 数 |
|---|---:|
| `soft_differ` | 47 |
| `identical` | 82 |
| `declared_in_some_runs_only` | 4 |
| `both_differ` | 5 |
| `hard_differ` | 3 |

| 名字 | 至少一轮声明了 | 三轮一致 |
|---|---:|---:|
| `arrive_before_meeting` | 3 | 3 |
| `avoid_early_departure` | 5 | 2 |
| `direct_only` | 2 | 1 |
| `flight_only` | 8 | 7 |
| `lowest_cost` | 77 | 35 |
| `prefer_flight` | 5 | 1 |
| `prefer_train` | 1 | 1 |
| `shortest_duration` | 17 | 3 |
| `train_only` | 8 | 2 |

声明不同的用例：

- `ct-human-h20241029143450090032` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost', 'shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143457494209` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143500599951` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143506461809` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['avoid_early_departure']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143510880048` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143524548106` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143528503277` · `both_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['direct_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['direct_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['shortest_duration']
- `ct-human-h20241029143530430866` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['avoid_early_departure', 'shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['avoid_early_departure', 'lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['avoid_early_departure']
- `ct-human-h20241029143543943871` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143546424651` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['prefer_flight']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143549773288` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143551657496` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['arrive_before_meeting'] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=['arrive_before_meeting'] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=['arrive_before_meeting'] soft=[]
- `ct-human-h20241029143553510979` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost', 'shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143602442546` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143606240237` · `both_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143608049345` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['flight_only'] soft=['prefer_flight']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['flight_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=['flight_only'] soft=['lowest_cost']
- `ct-human-h20241029143628653733` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['prefer_flight']
- `ct-human-h20241029143632058747` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143636163145` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143652031792` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143657365639` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['train_only'] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=['train_only'] soft=['lowest_cost']
- `ct-human-h20241029143701815147` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143704161138` · `both_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143709097610` · `hard_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=['train_only'] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=['train_only'] soft=[]
- `ct-human-h20241029143720725398` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['avoid_early_departure']
- `ct-human-h20241029143722458026` · `hard_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=['train_only'] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143733781691` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143736524841` · `both_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143741942032` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143743586105` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost', 'shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143750930343` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143759740039` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143803188730` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143811168339` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143812547929` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['shortest_duration']
- `ct-human-h20241029143818664124` · `both_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['train_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=['train_only'] soft=[]
- `ct-human-h20241029143824748448` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143826541906` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143828857993` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['prefer_flight']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143832205713` · `hard_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['flight_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143835520306` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143837173547` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143840382242` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ct-human-h20241029143842373194` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143846901147` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143853055859` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['shortest_duration']
- `ct-human-h20241029143854711129` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143904148477` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143906048411` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143908194169` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=['flight_only'] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=['flight_only'] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=['flight_only'] soft=['shortest_duration']
- `ct-human-h20241029143909714190` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost', 'shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['shortest_duration']
- `ct-human-h20241029143915014327` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']
- `ct-human-h20241029143916604779` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['shortest_duration']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['avoid_early_departure']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['avoid_early_departure']
- `ad-val-0018` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=[]
- `ad-val-0024` · `soft_differ`
  - `external-longtail-multiturn-live-r1-20260906`: hard=[] soft=['lowest_cost']
  - `external-longtail-multiturn-live-r2-20260906`: hard=[] soft=[]
  - `external-longtail-multiturn-live-r3-20260906`: hard=[] soft=['lowest_cost']

## 第 4 层 · 追问目标（粗版）

方法：`keyword-rules-v1`。核心事实（date, origin, destination, arrive_by）在整段话里出现即算问了；附带事实只在带问号或「吗 / 还是 / 需不需要」的句子里才算。

三轮都追问了的用例 179；核心事实集合一致 136；全集一致 101；只有附带事实不同 35；有的轮追问有的轮没有 4（与第 1 层的不一致重叠）。

第一轮终局动作（问 / 交付 / 越界）三轮相同 177/180。

| 问的事 | 至少一轮问了 | 三轮一致 | 一致率 |
|---|---:|---:|---:|
| `date` | 170 | 161 | 89.9% |
| `origin` | 10 | 170 | 95.0% |
| `destination` | 17 | 165 | 92.2% |
| `arrive_by` | 21 | 158 | 88.3% |
| `lodging` | 43 | 148 | 82.7% |
| `transport_mode` | 39 | 141 | 78.8% |
| `travelers` | 0 | 179 | 100.0% |
| `budget` | 7 | 172 | 96.1% |
| `intent_confirm` | 7 | 172 | 96.1% |

核心事实不同的 43 条（需人看是真差异还是关键词漏判）：

- `ct-human-h20241029143453343691`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：请问你们打算哪天从南京出发去成都、哪天返回？另外需要我帮忙订酒店吗？
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date, intent_confirm, lodging, transport_mode]：我先确认一下：您这条请求是**成都3天自由行的行程规划**（景点+美食推荐），而不是要我帮您订机票/酒店/火车票，对吗？  如果是行程规划，我可以帮您梳理一份3天成都游的路线建议。不过我需要先说明几点：  1. **交通**：您从南京出发，2人，去成都玩3天——但您没有说具体出发日期和返回日期。如果您需要我帮您查南京↔成都的机票/高铁，请告诉我出发日期（或"哪天必须到成都"）。  2. **酒店*
  - `external-longtail-multiturn-live-r3-20260906` [date, lodging, transport_mode]：这趟成都之行，你们打算哪天出发、哪天回来？另外，交通和住宿需要我一起安排吗（比如订高铁/机票、订酒店），还是只帮你规划景点和美食路线？
- `ct-human-h20241029143458760702`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你们计划哪天从南京出发去武汉？（3天行程，我需要出发日期才能查车票/机票和酒店）
  - `external-longtail-multiturn-live-r2-20260906` [date, lodging]：请问您计划哪天出发去武汉？（例如"9月12日出发"或"下周五出发"）另外，住宿需要我帮您一起安排订房吗？
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date, transport_mode]：我先确认一下：您是从南京出发去武汉玩3天，但还没有告诉我具体的出发日期和返回日期。这是安排交通和行程的前提。  在您告诉我日期之前，我可以先帮您梳理一下武汉"有历史感的区域 + 本地人常吃的特色美食"的玩法思路，等日期定了我立刻帮您查车票/机票。  **武汉有历史感的区域（建议串成一条线）：**  1. **汉口老租界区**（江汉路、黎黄陂路、吉庆街一带）—— 民国建筑群、老里份，历史感最浓，也是
- `ct-human-h20241029143508251643`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：你想让我帮你安排从南京到成都的交通（以及成都的住宿）吗？还是只是想了解成都适合和朋友一起玩的地方推荐？另外，你们打算哪天出发、哪天回来？
  - `external-longtail-multiturn-live-r2-20260906` [date, lodging]：你问的是成都适合和朋友一起玩的地方（景点/玩法推荐），这属于旅游攻略，不是差旅安排（订机票/酒店/高铁）。我可以帮你查南京到成都的交通和成都的酒店，但景点推荐我这边没有工具能查。需要我帮你查交通和酒店吗？另外，你们打算哪天出发、哪天回？
  - `external-longtail-multiturn-live-r3-20260906` [lodging]：你问的是成都适合和朋友一起玩的地方（景点/活动推荐），这属于旅游攻略，不是差旅安排（订机票、酒店、高铁）。我可以帮你查南京到成都的交通和成都的酒店，但景点推荐不在我的能力范围内。需要我帮你查交通或酒店吗？
- `ct-human-h20241029143518931601`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你们4个人打算哪天出发去杭州？（比如这周五、下周一，或者具体日期）
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：请问你们计划哪两天去杭州？（例如：9月12日出发、9月13日返回）
  - `external-longtail-multiturn-live-r3-20260906` [date, lodging, transport_mode]：我来帮你安排苏州到杭州的行程。不过我需要先确认几个关键信息才能搜索：  1. **出发日期**：你们打算哪天出发？（比如这周五、下周一，或具体日期） 2. **交通偏好**：4个人出行，高铁还是飞机？苏州到杭州距离不远，高铁通常更方便，但如果你有偏好请告诉我。 3. **住宿**：需要我帮你订杭州的酒店吗？（2天行程，通常住1晚）  请告诉我出发日期，我就可以开始搜索车票了。
- `ct-human-h20241029143522848652`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination]：四天里哪几天有空闲可以安排美食广场和大学游览？想去哪所大学（清华、北大、北师大等）？
  - `external-longtail-multiturn-live-r2-20260906` [date]：回程日期未定：四天行程，是 9月10日 还是 9月11日 返回深圳？需要我帮你搜返程机票吗？ 美食广场和大学想看哪几个？北京有簋街、王府井小吃街、南锣鼓巷等美食街区，大学可考虑清华、北大（海淀区）或人大等，告诉我偏好我可以帮你规划路线。
  - `external-longtail-multiturn-live-r3-20260906` [date]：返程是哪一天、从北京回深圳的日期还没定，需要你确认后我再搜返程票。
- `ct-human-h20241029143526214076`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination]：我来帮你规划这趟北京到苏州的3天旅行。不过先说明一下：我这边能帮你查的是**交通和酒店**的实时库存，景点和美食推荐我可以结合常识给你建议，但具体行程安排（去哪些景点、吃什么）需要你自己定。  先帮你查一下北京到苏州的交通。不过我需要确认一个关键信息：**你们打算哪天出发？** 这决定了我能查哪天的票。  在等你回复的同时，我先说明一下苏州值得去的景点和美食，供你参考：  **著名景点（3天可以这
  - `external-longtail-multiturn-live-r2-20260906` [date, destination]：我来帮你规划北京到苏州的3天旅行。不过先说明一下：我这边能帮你查的是**交通和酒店**的实际情况，景点和美食推荐我可以结合苏州的常识给你建议，但具体行程安排（每天去哪几个景点）需要你自己定。  先帮你查一下北京到苏州的交通，以及苏州的住宿情况。请问你们打算**哪天出发**？3天的话，出发日期定了我才能查具体的车票/机票和酒店。  另外，关于景点和美食，我可以先给你一些苏州的经典推荐：  **著名景
  - `external-longtail-multiturn-live-r3-20260906` [date]：你们计划哪天从北京出发去苏州？（比如"9月10号出发"或"这周五出发"）
- `ct-human-h20241029143532210869`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你打算哪天出发去北京？5天行程的话，回程日期也请一并告诉我（比如 9月10日出发、9月14日回）。
  - `external-longtail-multiturn-live-r2-20260906` [date, origin]：你打算哪天出发去北京？5天行程是从哪天开始算？（比如"9月10号出发"或"这周五走"）
  - `external-longtail-multiturn-live-r3-20260906` [date, origin]：你打算哪天出发去北京？5天行程是从哪天开始算？（比如「9月10日出发，玩到9月14日」）告诉我出发日期，我就能帮你查苏州到北京的车票/机票。
- `ct-human-h20241029143542560410`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, lodging]：明天是 2026-09-07。您要去广州玩 5 天，但您问的是"好玩的地方"——这是旅游景点推荐，不是差旅安排（订交通、订酒店）。  我这边能帮您做的是：查南京到广州的交通、以及广州的酒店。景点游玩攻略不在我的能力范围内。  不过我可以先帮您把交通和住宿查好。请问：  1. 您需要我帮您订**南京→广州**的交通吗？如果需要，您希望哪天出发、大概几点到？ 2. 需要帮您订**广州的酒店**吗（5 
  - `external-longtail-multiturn-live-r2-20260906` [lodging, transport_mode]：明天是 2026-09-07。你要去广州玩 5 天，这是休闲旅行，不是差旅安排——我的工具只能查交通和酒店库存，没法帮你找"好玩的地方"（景点、攻略这类内容）。  不过我可以帮你把行程的交通和住宿查出来。先确认一下你的需求：  1. **交通**：南京 → 广州，明天（9/7）出发，需要我查高铁/航班吗？ 2. **住宿**：5 天（9/7 入住，9/12 退房），需要我帮你订酒店吗？ 3. **
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, lodging]：明天是 2026-09-07。您要去广州玩 5 天，但您问的是"好玩的地方"——这是旅游景点推荐，不是差旅安排（订交通、订酒店）。  我这边能帮您做的是：查南京到广州的交通、以及广州的酒店。景点游玩攻略不在我的能力范围内。  不过我可以先帮您把交通和住宿查出来，您看需要吗？如果需要，请告诉我：  1. **交通**：明天（9月7日）从南京出发去广州，您希望大概几点到、或者几点出发？ 2. **住宿
- `ct-human-h20241029143543943871`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, lodging, origin, transport_mode]：我先确认一下这趟行程的关键信息，再帮你查交通和住宿。  目前我理解到的： - **出发地**：成都 - **目的地**：北京 - **人数**：4人 - **时长**：5天左右 - **兴趣**：名校氛围（清华、北大等）+ 历史文化（故宫、长城、胡同等）  在动手查票之前，有几个日期信息我还没法从你的话里确定，需要你告诉我：  1. **出发日期**：你们打算哪天从成都出发？（比如 9月10日、下
  - `external-longtail-multiturn-live-r2-20260906` [date]：请问你们计划哪天出发去北京？（比如 9月10日 出发，玩5天左右）
  - `external-longtail-multiturn-live-r3-20260906` [date, destination, lodging, origin, transport_mode]：我先确认一下这趟行程的关键信息，再帮你查交通和住宿。  目前我理解到的： - **出发地**：成都 - **目的地**：北京 - **人数**：4人 - **天数**：5天左右 - **兴趣**：名校氛围（清华、北大等）+ 历史文化（故宫、长城、胡同等）  不过有几个信息我还需要你确认，才能帮你查可预订的交通和住宿：  1. **出发日期**：你们打算哪天从成都出发？（比如 9月几号） 2. **
- `ct-human-h20241029143627200710`
  - `external-longtail-multiturn-live-r1-20260906` [date]：您说的中秋假期，是指 2026 年 9 月 25 日（周五）出发、玩到 27 日（周日）这三天吗？还是您有具体的出发日期？
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：您说的中秋假期3天，具体是哪三天出发和返回？比如是9月25日（周五）出发、9月27日（周日）返回吗？
  - `external-longtail-multiturn-live-r3-20260906` [date]：您说的中秋假期3天，是指 9月25日（周五）出发、9月27日（周日）返回吗？还是您有具体的出发日期？
- `ct-human-h20241029143630351554`
  - `external-longtail-multiturn-live-r1-20260906` [lodging]：住宿是否需要我帮你订酒店（还是你们自己安排）？ 两天行程想侧重哪类：市区历史文化（磁器口/洪崖洞/三峡博物馆）还是凉快风景（南山/武隆仙女山）？
  - `external-longtail-multiturn-live-r2-20260906` [date, lodging]：返程日期是否按周日 9/13 安排？还是想周六回或周一回？ 是否需要我帮你查重庆的酒店（2人、2晚）？
  - `external-longtail-multiturn-live-r3-20260906` [lodging]：是否需要我帮你搜索并预订重庆的酒店（2人、住1晚）？
- `ct-human-h20241029143632058747`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：您提到在北京转机、想安排一天行程，但有几个关键信息我还没法确定：1）转机的具体日期是哪天（几号到北京、几号离开）？2）是否需要我帮您安排重庆到北京、以及北京到下一站的机票/交通？3）住宿是否需要安排（还是当天转机不过夜）？
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：您提到在北京转机、想安排一天行程探索景点，但还没有告诉我具体的日期和航班时间。请问您转机的具体日期是哪天？以及当天需要在几点前回到机场（或几点前必须结束行程）？这样我才能帮您查交通和安排。
  - `external-longtail-multiturn-live-r3-20260906` [lodging, transport_mode]：您提到在北京转机、想安排一天行程探索景点，但这条请求更像是当地游玩规划，而不是需要我搜索交通/酒店库存的差旅安排。请问您具体需要我帮您做什么？比如：查重庆到北京、以及北京出发的转机航班/高铁，还是帮您订北京的酒店？
- `ct-human-h20241029143639901784`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你希望哪一天出发、哪一天返回？（停留两天，本周每天都有直飞航班）
  - `external-longtail-multiturn-live-r2-20260906` [date]：「本周」我按 9/7 这一周理解，去程定在 9/13（周日）、返程 9/15（周二），停留两天。如果实际出发日不同，请告诉我具体日期。
  - `external-longtail-multiturn-live-r3-20260906` [（没读出在问什么）]：「本周」具体是哪两天出发/返回？我按 9/13 出发、9/15 返回理解，如需调整请告知。
- `ct-human-h20241029143645157524`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：你这次去杭州主要是看同学、逛浙大，周末两天。请问你打算哪天出发、哪天回来？（比如这周六去、周日回，还是别的日期？）另外，交通和住宿需要我帮你一起查吗？
  - `external-longtail-multiturn-live-r2-20260906` [date]：你这次去杭州主要是看同学、逛浙大，周末两天。请问你打算哪天出发、哪天回来？（比如这周六去、周日回，还是别的日期？）另外，需要我帮你安排苏州到杭州的交通吗？
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date, lodging, transport_mode]：你这次去杭州主要是看同学、逛浙大，周末两天。请问你希望我帮你安排哪些部分？比如：苏州到杭州的往返交通（高铁/大巴），还是杭州当地的住宿，或者只是想要一份浙大周边两天的游玩建议？另外，你打算哪个周末出发、大概几点到杭州？
- `ct-human-h20241029143730769592`
  - `external-longtail-multiturn-live-r1-20260906` [date, transport_mode]：回程杭州→南京当天没有航班库存，是否考虑改坐高铁返回，或改到次日返回？ Hangzhou→Nanjing 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。
  - `external-longtail-multiturn-live-r2-20260906` [date, transport_mode]：回程杭州→南京当天无航班库存，是否改乘高铁返回？或接受次日返回？ Hangzhou→Nanjing 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date, transport_mode]：返程（杭州→南京）当天航班无库存，是否改乘高铁？或告知可接受的最晚返程时间/是否次日返回 Hangzhou→Nanjing 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。
- `ct-human-h20241029143832205713`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：您说的"中秋节"出发，是指 2026 年 9 月 25 日（周五，农历八月十五）当天从杭州飞广州吗？还是想提前一两天出发、在中秋假期里玩满五天？
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：您说的"中秋节"出发，是指中秋节当天（2026年9月25日，周五）出发，还是想在中秋假期（9月25日-27日）的某一天出发？另外，两个孩子去海洋馆和动物园，广州有长隆野生动物世界和正佳极地海洋世界，我可以帮您查交通，但景点门票和具体安排需要您自己决定，需要我一起查吗？
  - `external-longtail-multiturn-live-r3-20260906` [date]：中秋节是农历八月十五，2026年对应公历9月25日（周五）。请问您希望9月25日当天从杭州出发，还是想提前一天（9月24日）出发、25日一早就到广州开始玩？另外返程日期我按5天行程推算是9月29日（周二）回杭州，可以吗？
- `ct-human-h20241029143834377206`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：请问您计划哪天出发去南京？当天有需要几点前到达的安排吗（比如会议时间）？
  - `external-longtail-multiturn-live-r2-20260906` [date]：请问这趟广州到南京的行程，具体是哪一天出发？（旅行天数1天，我需要确认出发日期才能帮您查票）
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date]：我来帮您安排从广州到南京的行程。不过我需要先确认几个关键信息才能搜索：  1. **出发日期**：您计划哪天出发？（参考时间是 2026-09-06） 2. **到达时间要求**：南京那边有需要在某个时间点前到达的安排吗（比如会议、活动）？  您提到"不当特种兵、相对轻松一点"，我会在搜索结果里优先考虑出发时间不太早、行程不那么紧凑的选项。  请告诉我出发日期和到达时间要求，我马上为您搜索。
- `ct-human-h20241029143840382242`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging, transport_mode]：我先确认一下这趟行程的关键信息，然后帮你查交通。  你提到"3个人去北京玩5天"，但还没有说**出发日期**和**返程日期**。这是规划旅程必须的信息。  另外，关于"体验历史文化"，我可以帮你梳理北京5天的历史文化行程建议（故宫、长城、天坛、颐和园、胡同等），但**交通和住宿的预订**需要你告诉我具体日期。  请问：  1. **出发日期**是哪天？（比如"9月10日出发"） 2. 需要我帮你*
  - `external-longtail-multiturn-live-r2-20260906` [date, origin]：您计划哪天从南京出发去北京？5天行程大概想从哪天开始（比如9月某日出发）？
  - `external-longtail-multiturn-live-r3-20260906` [date]：请问你们计划哪天从南京出发去北京？（5天行程，我需要出发日期才能帮你查交通和安排）
- `ct-human-h20241029143846901147`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, lodging, transport_mode]：我先确认一下这趟行程的关键信息，然后帮你查交通和住宿。  不过有几个点需要先跟你对齐：  1. **出发日期**：你打算哪天从南京出发去北京？（5天行程，我需要知道具体日期才能查票） 2. **住宿**：5天4晚，需要我帮你安排北京的酒店吗？还是你自己订？ 3. **交通方式**：南京到北京，你倾向高铁还是飞机？有没有时间上的硬要求（比如必须几点前到）？  你先把出发日期告诉我，我就能开始查票了。
  - `external-longtail-multiturn-live-r2-20260906` [date, lodging]：我先确认一下这趟行程的范围：您是从南京出发去北京玩5天，需要我帮您安排交通（南京↔北京）和住宿吗？还是只安排其中一部分？  另外，5天具体是哪几天出发、哪天回？您有偏好的出发日期吗？
  - `external-longtail-multiturn-live-r3-20260906` [date]：您计划哪天从南京出发去北京？（5天行程，比如9月某天出发，我好按这个日期查往返票）
- `ct-human-h20241029143851561679`
  - `external-longtail-multiturn-live-r1-20260906` [date]：国庆假期你计划哪 4 天去北京？比如 10 月 1 日出发、10 月 4 日返回，还是其他日期？确定后我就能帮你查杭州往返北京的交通和北京的住宿。
  - `external-longtail-multiturn-live-r2-20260906` [date, destination, origin]：国庆假期具体是哪几天出发、哪几天回，你还没说清楚，所以我没法直接搜到车票/机票。不过我可以先把能确定的都准备好，只差你一个日期。  先跟你确认一下这趟行程的关键信息：  **已经明确的：** - 出发地：杭州 → 目的地：北京 - 人数：2 人（你和同学） - 天数：4 天 - 预算：4000 元（两人合计） - 住宿：要经济划算的 - 玩法：想找当地人更推荐的地方（这个我可以给你建议，但订票订房
  - `external-longtail-multiturn-live-r3-20260906` [date]：国庆假期具体是哪几天出发、哪天回？比如「10月1日出发、10月4日回」这样，我好按实际日期查车票/机票和住宿。
- `ct-human-h20241029143900796443`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, origin, transport_mode]：您还没有告诉我具体的出发日期，所以我暂时无法查到高铁班次。不过我可以先把行程框架和需要您确认的信息整理好。  **已明确的行程信息：** - 出发地：北京 → 目的地：苏州 - 人数：2 人（您和女朋友） - 天数：4 天 - 交通：往返都坐高铁（硬性要求） - 预算：尽量低（偏好）  **需要您补充的信息：** 1. **出发日期**：您打算哪天从北京出发？（例如"9月10日出发"或"下周三出发
  - `external-longtail-multiturn-live-r2-20260906` [date]：您计划哪天从北京出发去苏州？（玩四天，往返都坐高铁，我会按最低价帮您找）
  - `external-longtail-multiturn-live-r3-20260906` [date]：请问你们计划哪天从北京出发去苏州？（玩四天，我需要出发日期才能查高铁票）
- `ct-human-h20241029143913181191`
  - `external-longtail-multiturn-live-r1-20260906` [lodging]：这趟是问南京的景点推荐，不是要我帮你订车票/酒店。请问你需要我帮你安排苏州到南京的往返交通（或住宿）吗？还是只想听景点推荐？
  - `external-longtail-multiturn-live-r2-20260906` [date, intent_confirm, lodging]：这趟是问南京的景点推荐，不是要我帮你订车票/酒店吧？如果是想让我安排苏州到南京的往返交通和住宿，请告诉我出发和返回的日期（比如这周五出发、周日回），我再帮你查。如果只是想听景点推荐，我可以直接给你列几个南京值得去的江南水乡/园林景点。
  - `external-longtail-multiturn-live-r3-20260906` [date]：你问的是南京的景点推荐，这属于旅游攻略，不是差旅安排（订交通/酒店）。我可以帮你查苏州到南京的交通和住宿，但景点推荐我这边没有工具能查。需要我帮你安排苏州⇄南京的往返交通吗？如果需要，请告诉我具体出发和返回的日期（比如这周五出发、周日回）。
- `ct-human-h20241029143918467169`
  - `external-longtail-multiturn-live-r1-20260906` [date]：你们打算哪天出发去上海？(玩三天，我需要确定出发日期才能查车票)
  - `external-longtail-multiturn-live-r2-20260906` [date]：你们打算哪天出发去上海？（比如 9 月 7 日、8 日……）告诉我出发日期，我就能把南京到上海的交通也一起查好。
  - `external-longtail-multiturn-live-r3-20260906` [date, origin]：你们打算哪天从南京出发去上海？（玩三天，但没说从哪天开始）另外需要我一起安排返程票吗？
- `ct-human-h20241029143923769644`
  - `external-longtail-multiturn-live-r1-20260906` [date, lodging]：你们计划哪天出发去广州？（5天行程，我需要出发日期才能帮你查南京到广州的交通）另外，需要我帮你一起订广州的酒店吗？
  - `external-longtail-multiturn-live-r2-20260906` [date, lodging]：我先确认一下：您说的是从南京出发去广州玩5天，但还没有告诉我具体的出发日期和返程日期。这是规划行程（尤其是订交通）必需的信息。  在您告诉我日期之前，我可以先帮您梳理一下广州5天的特色热门玩法方向，供您参考：  **广州5天经典玩法（当地特色+热门）**  - **Day 1 老城文化**：陈家祠（岭南建筑精华）→ 上下九步行街 → 沙面岛（欧陆风情）→ 珠江夜游 - **Day 2 城市地标**
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date, lodging, transport_mode]：我先确认一下：您是从南京出发去广州玩5天，但还没有告诉我具体的出发日期和返程日期。这是规划行程（尤其是订交通）必需的信息。  在您告诉我日期之前，我可以先帮您梳理一下广州5天的特色热门玩法方向，供您参考：  **广州5天经典玩法（特色热门）**  - **Day 1 老城文化**：陈家祠（岭南建筑精华）→ 上下九步行街 → 沙面岛（欧陆风情） - **Day 2 珠江新城+地标**：广州塔（小蛮腰
- `ad-val-0002`
  - `external-longtail-multiturn-live-r1-20260906` [date]：What date do you need to be in New York for the wedding?
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：What date is the wedding, and do you need to arrive by a specific time (e.g., the day before, or by a certain hour on the wedding day)?
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date]：What date is the wedding, and do you need to arrive by a specific time that day?
- `ad-val-0004`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：What date does the pioneer festival take place, and do you need to arrive by a specific time that day?
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：What date does the pioneer festival take place, and do you need to arrive by a specific time that day?
  - `external-longtail-multiturn-live-r3-20260906` [date]：What date do you need to arrive in Detroit (DTW) for the pioneer festival?
- `ad-val-0007`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, transport_mode]：Hello David! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for tr
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：Hello David! I'm here to help with your corporate travel arrangements.   What trip can I help you plan? Please share the details — where you're going, dates, and any preferences (like direct flights o
  - `external-longtail-multiturn-live-r3-20260906` [destination]：Hello David! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
- `ad-val-0008`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：Hello Gary! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
  - `external-longtail-multiturn-live-r2-20260906` [destination]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r3-20260906` [date, destination, transport_mode]：Hello Gary! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for tr
- `ad-val-0010`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, origin]：I'd be happy to help you book a flight, Anna. To search for real options, I need to know a few details about your trip:  1. **Where are you flying from and to?** (origin and destination cities) 2. **W
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date, transport_mode]：I'd be happy to help you book a flight, Anna. Could you tell me a bit more about your trip?  - Where are you flying **from** and **to**? - What **date** do you need to travel (or do you need to arrive
  - `external-longtail-multiturn-live-r3-20260906` [date, destination, origin, transport_mode]：I'd be happy to help you book a flight, Anna. To search for real options, I need to know a few details about your trip:  1. **Where are you flying from and to?** (origin and destination cities) 2. **W
- `ad-val-0013`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, origin, transport_mode]：I'd be happy to help you book a ticket, Michael. To search for options, I need a few details about your trip:  1. **Where are you traveling from and to?** (origin and destination cities) 2. **When do 
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date, destination]：I need a bit more to go on before I can search anything. Could you tell me:  1. **Where are you traveling from and to?** (city names) 2. **What date do you need to be there, and by what time?** (e.g.,
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date, destination, origin, transport_mode]：I'd be happy to help you book a ticket, Michael. To search for the right options, I need a few details about your trip:  1. **Where are you traveling from and to?** (origin and destination cities) 2. 
- `ad-val-0014`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：Hello Jennifer! I'm here to help you plan your corporate travel. What trip can I help you arrange?
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：Hello Jennifer! I'm here to help with your corporate travel arrangements. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260906` [date, destination, transport_mode]：Hello Jennifer! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference fo
- `ad-val-0017`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：I can help with that, but I need a couple of details to search real inventory:  1. **Travel date** — when do you want to fly? (And do you need to arrive by a certain time that day?) 2. **Return** — is
  - `external-longtail-multiturn-live-r2-20260906` [date]：What date would you like to fly from Houston (HOU) to Denver (DEN)?
  - `external-longtail-multiturn-live-r3-20260906` [date]：Which day would you like to fly from Houston (HOU) to Denver (DEN)?
- `ad-val-0018`
  - `external-longtail-multiturn-live-r1-20260906` [date]：What date would you like to travel from ORD to SFO?
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：I can help with that. I need to know the date you'd like to travel — do you have a specific day in mind, or a deadline you need to arrive by?
  - `external-longtail-multiturn-live-r3-20260906` [date]：What date would you like to travel from ORD to SFO?
- `ad-val-0019`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination]：I can help with that, but I need to know where the meeting is and when you need to be there. Could you tell me:  1. **Destination city** — where is the meeting? 2. **Date and time** — when do you need
  - `external-longtail-multiturn-live-r2-20260906` [date, destination]：I can help with that, Gary. I need a couple of details to search the right flights:  1. **Where is the meeting?** (destination city) 2. **When do you need to be there?** (date and time of the meeting)
  - `external-longtail-multiturn-live-r3-20260906` [date]：Where is the meeting, and what date/time do you need to be there by?
- `ad-val-0022`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：I can help with that, but I need a couple of details to search for real options:  1. **What date** do you want to fly from JFK to IAH? 2. **What time** do you need to arrive by (or is there a specific
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：What date do you need to fly from JFK to IAH, and is there a time you need to arrive by (e.g., a meeting or event)?
  - `external-longtail-multiturn-live-r3-20260906` [date]：I can help search for flights from JFK to IAH, but I need to know your travel date. Could you tell me when you'd like to fly?
- `ad-val-0030`
  - `external-longtail-multiturn-live-r1-20260906` [destination, origin]：I can help with that, but let me flag something first: SFO and OAK are both airports in the San Francisco Bay Area, only about 20 miles apart. There are no commercial flights between them — this is a 
  - `external-longtail-multiturn-live-r2-20260906` [date]：您想哪天从 SFO 飞往 OAK？请告诉我具体的出发日期（或需要到达的日期/时间）。
  - `external-longtail-multiturn-live-r3-20260906` [destination, origin]：I can help with that, but let me flag something first: SFO and OAK are both airports in the San Francisco Bay Area, only about 20 miles apart. There are no commercial flights between them — this is a 
- `ad-val-0031`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：What date is the seminar, and what time do you need to be in Denver by?
  - `external-longtail-multiturn-live-r2-20260906` [arrive_by, date]：What date does the seminar take place, and do you need to arrive by a specific time that day?
  - `external-longtail-multiturn-live-r3-20260906` [date]：What date does the seminar start (or what date/time do you need to be in Denver by)?
- `ad-val-0033`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date]：What date do you need to travel from SFO to HOU, and is there a time you need to arrive by (e.g., a meeting)?
  - `external-longtail-multiturn-live-r2-20260906` [date]：What date would you like to fly from SFO to HOU?
  - `external-longtail-multiturn-live-r3-20260906` [date]：What date would you like to fly from SFO to HOU?
- `ad-val-0036`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), not corporate travel. I can only help arrange business travel. Is there a work-related trip I can help you with instead
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：This looks like a personal leisure trip (visiting Madame Tussauds with friends), which is outside what I can arrange. I handle corporate travel only. Is there a business trip I can help you with inste
  - `external-longtail-multiturn-live-r3-20260906` [date, destination]：This looks like a personal trip to Madame Tussauds with friends, not corporate travel, so I can't book it. If you do have a business trip to arrange (flights, trains, or hotels), let me know the desti
- `ad-val-0037`
  - `external-longtail-multiturn-live-r1-20260906` [arrive_by, date, destination, transport_mode]：I'm Linda Smith — nice to meet you. I can help you plan corporate travel: flights, trains, and hotels for your business trips.  What trip can I help you arrange? For example, where are you headed, on 
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：I'm Linda Smith. How can I help with your corporate travel today?
  - `external-longtail-multiturn-live-r3-20260906` [（没读出在问什么）]：I'm Linda Smith — I'm here to help arrange your corporate travel. What trip can I help you plan?
- `ad-val-0039`
  - `external-longtail-multiturn-live-r1-20260906` [date, destination, transport_mode]：Hi Gary! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, cities, and any preferences (like direct flights only, or a preference for train vs. fl
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：Hi Gary! I'm here to help arrange your corporate travel. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260906` [date, destination]：Hi Gary! I'm here to help with your corporate travel. What trip can I arrange for you — where are you headed, and when?
- `ad-val-0041`
  - `external-longtail-multiturn-live-r1-20260906` [（没读出在问什么）]：I'm Raymond's travel assistant. What trip can I help you arrange?
  - `external-longtail-multiturn-live-r2-20260906` [（没读出在问什么）]：I'm Raymond's travel assistant. What trip can I help you plan?
  - `external-longtail-multiturn-live-r3-20260906` [arrive_by, date]：Hi Raymond, nice to meet you. What trip can I help you arrange? Please share the cities, dates, and any preferences (e.g., direct flights only, prefer train, must arrive by a certain time).
- `ad-val-0042`
  - `external-longtail-multiturn-live-r1-20260906` [destination]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details of your trip?
  - `external-longtail-multiturn-live-r2-20260906` [date, destination, transport_mode]：Hello Sharon! I'm here to help you plan your corporate travel. Where are you headed, and what are the details — dates, destinations, and any preferences (like direct flights only, or a preference for 
  - `external-longtail-multiturn-live-r3-20260906` [date, destination, transport_mode]：Hello Sharon! I'm here to help arrange your corporate travel. Where are you headed, and what are the details — dates, times, and any preferences (like direct flights only or a preference for train)?

## 过程（不设门禁）· 同一条用例三轮各花了多少步

次数不是决策，按离散度（三轮最大值减最小值）报；超过阈值的用例列出来给人看。

| 指标 | 用例 | 三轮相同 | 离散度中位数 | 最大 | ≥ 阈值 | 各轮合计 |
|---|---:|---:|---:|---:|---:|---|
| 模型调用次数 | 185 | 96 | 0 | 4 | 2（阈值 4） | 632 / 625 / 617 |
| 成功搜索次数 | 185 | 170 | 0 | 5 | 8（阈值 2） | 318 / 310 / 304 |
| 被宿主拒掉的搜索次数 | 185 | 98 | 0 | 4 | 6（阈值 3） | 161 / 151 / 145 |
| 对话轮数 | 185 | 176 | 0 | 2 | 9（阈值 1） | 358 / 355 / 357 |

模型调用次数离散度最大的用例（值按轮次顺序）：

- `ct-human-h20241029143639901784`：[7, 3, 3]（离散度 4）
- `ad-val-0015`：[3, 7, 3]（离散度 4）

被宿主拒掉的搜索次数离散度最大的用例（值按轮次顺序）：

- `ct-human-h20241029143646523600`：[4, 0, 0]（离散度 4）
- `ct-human-h20241029143648613072`：[4, 0, 2]（离散度 4）
- `ct-human-h20241029143738057260`：[0, 0, 4]（离散度 4）
- `ct-human-h20241029143900796443`：[4, 2, 0]（离散度 4）
- `ad-val-0015`：[0, 4, 0]（离散度 4）
- `ct-human-h20241029143533982475`：[0, 3, 0]（离散度 3）

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
| `external-longtail-multiturn-live-r2-20260906` | `534e594300e0f35c…` | `b57b7265b475a107` | 有 | 有 | 有 |
| `external-longtail-multiturn-live-r3-20260906` | `c0a73b7129547d28…` | `b3e896639aa99abf` | 有 | 有 | 有 |
