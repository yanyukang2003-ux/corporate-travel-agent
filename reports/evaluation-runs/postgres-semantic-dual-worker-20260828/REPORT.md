# 真实 PostgreSQL 上的语义任务并发验收

- started_at: `2026-08-27T23:39:23.729842+00:00`
- runner: `postgres-semantic-dual-worker-v1`
- database: `travel_agent_semantic_dual`（独立库，不碰主库）
- billed model calls: **0**（脚本化语义替身）
- provider: `mock`（不碰 Duffel / LiteAPI）
- result: **2/2 PASS**

断言的是**任何交错都必须成立**的不变量，不是某一种先后顺序。

| ID | Result | Title |
|---|---|---|
| SD-01 | PASS | 两个进程同时追加消息：账本不丢轮次、失败方拿到并发冲突 |
| SD-02 | PASS | 到期的语义任务重试只被一个 worker 领走 |

### SD-01 — 两个进程同时追加消息：账本不丢轮次、失败方拿到并发冲突

- ✅ `at_least_one_succeeded` — [{'message': '上午十点前要到', 'ok': False, 'error': 'ConcurrentUpdateError', 'detail': 'Task semantic-concurrent-followup was updated concurrently'}, {'message': '改成下午三点前到', 'ok': True, 'state': 'NEEDS_CLARIFICATION'}]
- ✅ `no_lost_or_duplicated_turn` — expected 2 user turns, stored 2: ['8月5日从北京去上海，想请你帮我安排', '改成下午三点前到']
- ✅ `no_duplicate_turn` — ['8月5日从北京去上海，想请你帮我安排', '改成下午三点前到']
- ✅ `seed_turn_survived` — ['8月5日从北京去上海，想请你帮我安排']
- ✅ `loser_saw_a_concurrency_error` — [{'message': '上午十点前要到', 'ok': False, 'error': 'ConcurrentUpdateError', 'detail': 'Task semantic-concurrent-followup was updated concurrently'}]
- ✅ `entrypoint_still_semantic` — semantic
- ✅ `decision_history_matches_turns` — 2

### SD-02 — 到期的语义任务重试只被一个 worker 领走

- ✅ `seeded_waiting_for_provider` — WAITING_FOR_PROVIDER
- ✅ `both_workers_reported` — [{'worker_id': 'worker-b', 'processed': []}, {'worker_id': 'worker-a', 'processed': ['semantic-concurrent-retry']}]
- ✅ `exactly_one_claimed` — ['worker-a']
- ✅ `entrypoint_still_semantic` — semantic
- ✅ `no_second_interpretation` — 1

