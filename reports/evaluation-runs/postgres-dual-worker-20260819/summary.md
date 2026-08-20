# Postgres dual-worker acceptance

- passed: **True**

- [PASS] `postgres_ready` — `alembic upgrade head`
- [PASS] `both_workers_exited_0` — `[{'returncode': 0, 'stdout': '{"worker_id": "worker-a", "processed": [], "live_hash": "fc9442205d14e1fe9598e4618c2da45331e08b737d4261218eaca6a5d28d0202"}\n', 'stderr': ''}, {'returncode': 0, 'stdout': '{"worker_id": "worker-b", "processed": ["lease-race-45f8179de2"], "live_hash": "fc9442205d14e1fe9598e4618c2da45331e08b737d4261218eaca6a5d28d0202"}\n', 'stderr': ''}]`
- [PASS] `single_worker_provider_job` — `{'search_transport': 2, 'calls': [{'name': 'search_transport', 'pid': 32014, 'at': '2026-08-19T12:31:29.193233+00:00'}, {'name': 'search_transport', 'pid': 32014, 'at': '2026-08-19T12:31:29.206212+00:00'}, {'name': 'search_hotels', 'pid': 32014, 'at': '2026-08-19T12:31:29.229303+00:00'}], 'search_hotels': 1}`
- [PASS] `final_not_double_searching` — `WAITING_FOR_USER`
- [PASS] `hold_claimed` — ``
- [PASS] `takeover_ok` — ``
- [PASS] `stale_write_rejected` — `Task lease-hold-23ee5e6135 was updated concurrently`
- [PASS] `explain_rows_ge_100k` — `100002`
- [PASS] `explain_used_index` — `{'row_count': 100002, 'trip_tasks_node_types': ['Bitmap Heap Scan'], 'used_index': True, 'seq_scan': False, 'plan_file': '/Users/yukangyan/Downloads/corporate-travel-agent/reports/evaluation-runs/postgres-dual-worker-20260819/explain.json'}`
