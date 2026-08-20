# Uncovered-types acceptance (§18.3 A–E)

- run_id: `uncovered-types-20260819`
- started_at: `2026-08-19T11:51:15.698350+00:00`
- passed: **23/23**

## A-01 Compliant select revalidates then hands off — PASS
- [PASS] `state` — `READY_FOR_HANDOFF`
- [PASS] `booking_intent`
- [PASS] `revalidate_before_intent`
- [PASS] `handoff_not_expired`

## A-02 Revalidation price change cannot hand off old price — PASS
- [PASS] `state` — `RECONFIRMATION_REQUIRED`
- [PASS] `no_booking_intent`

## A-03 Sold-out revalidation cannot hand off — PASS
- [PASS] `state` — `RECONFIRMATION_REQUIRED`
- [PASS] `no_booking_intent`

## A-04 Expired handoff is not given to the user — PASS
- [PASS] `state` — `PROVIDER_FAILED`
- [PASS] `no_booking_intent`
- [PASS] `mentions_expired`

## A-05 Tool budget during revalidation cannot skip revalidate — PASS
- [PASS] `state` — `TOOL_BUDGET_EXHAUSTED`
- [PASS] `no_revalidate_tool`
- [PASS] `no_booking_intent`

## B-01 Revising dates after approval invalidates the old approval — PASS
- [PASS] `old_invalidated` — `INVALIDATED`
- [PASS] `approval_cleared`
- [PASS] `request_v2`

## B-02 Expired approval cannot be applied — PASS
- [PASS] `error` — `The approval has expired`
- [PASS] `status`
- [PASS] `state` — `WAITING_FOR_USER`

## A-06 Live HTTP select is idempotent and does not mint a second intent — PASS
- [PASS] `select_200` — `200`
- [PASS] `handoff` — `READY_FOR_HANDOFF`
- [PASS] `revalidated`
- [PASS] `second_rejected` — `409`
- [PASS] `same_intent`

## A-07 Selecting a second option after handoff is rejected — PASS
- [PASS] `first_handoff`
- [PASS] `swap_rejected` — `409`

## B-03 Live approval requires reason, ignores chat, blocks swap, invalidates on revise — PASS
- [PASS] `reason_required` — `409`
- [PASS] `waiting` — `WAITING_FOR_APPROVAL`
- [PASS] `chat_rejected` — `409`
- [PASS] `cannot_swap` — `409`
- [PASS] `admin_forbidden` — `403`
- [PASS] `revise_ok` — `200`
- [PASS] `approval_cleared`

## B-04 Assigned manager M2001 can approve a live exception — PASS
- [PASS] `approved_200` — `200`
- [PASS] `approver`
- [PASS] `not_pending` — `READY_FOR_HANDOFF`

## C-01 AUTH live: login, 404 not 403, approver cannot create, bad token — PASS
- [PASS] `auth_enabled`
- [PASS] `unauth_401` — `401`
- [PASS] `bad_login_401` — `401`
- [PASS] `outsider_404` — `404`
- [PASS] `outsider_not_403` — `404`
- [PASS] `approver_cannot_create` — `403`
- [PASS] `admin_can_create`
- [PASS] `tampered_401` — `401`

## D-01 Employee workspace hides the approval queue — PASS
- [PASS] `plan` — `智能规划
我的差旅
差旅政策`
- [PASS] `no_approvals` — `智能规划
我的差旅
差旅政策`

## D-02 Clicking confirm revalidates via the live API — PASS
- [PASS] `ui_handoff` — `可交接
•••`
- [PASS] `api_handoff` — `READY_FOR_HANDOFF`

## D-03 Exception option blocks confirm until a business reason is filled — PASS
- [PASS] `disabled_without_reason`
- [PASS] `waiting` — `WAITING_FOR_APPROVAL`
- [PASS] `ui_approval` — `待审批
•••`

## D-04 Clarification panel click submits a structured option token — PASS
- [PASS] `seed_clarify` — `{'task_id': 'b0b40d9b-8bae-4a9d-b9e0-b523b0e5b611', 'state': 'NEEDS_CLARIFICATION', 'questions': [{'id': 'times', 'header': '时间', 'question': '请补充行程时间：最早出发时间、最晚到达/会议时间。也可选下方常用模板。', 'multi_select': False, 'slots': ['departure_after', 'arrive_by'], 'options': [{'label': '明天出差当天回', 'description': '单程：次日上午出发，当晚前到达；不安排返程', 'value': 'template:day_trip'}, {'label': '明天去后天回', 'description': '往返：次日出发，第三天返回', 'value': 'template:overnight'}]}], 'missing': ['departure_after', 'arrive_by'], 'failure': None}`
- [PASS] `button_present`
- [PASS] `moved_on` — `NO_FEASIBLE_OPTION`
- [PASS] `token_or_search` — `['local_parser:applied', 'clarification:template_overnight (origin Asia/Shanghai, dest Asia/Shanghai)']`

## D-05 Exhausted clarification can submit the structured form and search — PASS
- [PASS] `form_shown`
- [PASS] `searched` — `WAITING_FOR_USER`
- [PASS] `has_options`

## D-06 Approver M2001 sees the live inbox and cannot plan a new trip — PASS
- [PASS] `has_inbox` — `待我审批
1
团队差旅
差旅政策`
- [PASS] `no_plan` — `待我审批
1
团队差旅
差旅政策`

## D-07 Admin workspace has no approval decision entry — PASS
- [PASS] `no_plan` — `任务总览
政策管理
审计与系统`
- [PASS] `no_approvals` — `任务总览
政策管理
审计与系统`
- [PASS] `has_audit` — `任务总览
政策管理
审计与系统`

## D-08 Narrow viewport still renders the employee workspace — PASS
- [PASS] `sidebar`
- [PASS] `login_passed`

## E-exhaust Provider delayed-recovery scenario exhaust — PASS
- [PASS] `exit_0`
- [PASS] `summary_passed` — `{'exhaust': {'passed': True, 'failed_checks': []}}`

## E-circuit Provider delayed-recovery scenario circuit — PASS
- [PASS] `exit_0`
- [PASS] `summary_passed` — `{'circuit': {'passed': True, 'failed_checks': []}}`

## E-recover Provider delayed-recovery scenario recover — PASS
- [PASS] `exit_0`
- [PASS] `summary_passed` — `{'recover': {'passed': True, 'failed_checks': []}}`

