"""语义任务的落库、重启恢复与 Provider 失败重试。

之前的持久化测试全部走结构化或旧链路入口，语义任务是否能完整存下来、进程重启后
能不能接着走，一条都没测过。本文件补这块：

- 语义判定和入口标记要一起落库，重启后仍在；
- 重启后的任务仍然只认语义路由，旧路由必须拒绝；
- Provider 搜索失败重试期间不得重新解释一遍用户意图；
- 延迟重试、进程中断恢复在语义任务上同样生效。

用 SQLite 文件库模拟重启（和既有持久化测试一致），假模型不联网、不花钱。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from semantic_fixtures import READY_MESSAGE, ScriptedSemanticModel, semantic_decision

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import TaskState, ToolCallStatus
from corporate_travel_agent.domain.models import ToolCallRecord
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

FIXED_NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _database_url(tmp_path, name: str) -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def _new_repository(tmp_path, name: str) -> SQLAlchemyTaskRepository:
    repository = SQLAlchemyTaskRepository(_database_url(tmp_path, name))
    repository.create_schema()
    return repository


def _semantic_workflow(repository, model: ScriptedSemanticModel, **kwargs):
    workflow, provider = build_demo_system(
        semantic_language_model=model,
        clock=lambda: FIXED_NOW,
        task_repository=repository,
        **kwargs,
    )
    return workflow, provider


def test_semantic_decision_and_entrypoint_survive_a_repository_restart(tmp_path) -> None:
    """语义链路独有的留痕必须真的存进库，而不是只活在内存里。"""
    repository = _new_repository(tmp_path, "semantic.db")
    model = ScriptedSemanticModel([semantic_decision()])
    workflow, _ = _semantic_workflow(repository, model)

    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-persisted"
    )
    assert task.state is TaskState.WAITING_FOR_USER
    event_count = len(repository.events(task.task_id))
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(_database_url(tmp_path, "semantic.db"))
    restored = restarted.get("semantic-persisted")

    assert restored.metadata["intent_entrypoint"] == "semantic"
    decision = restored.metadata["semantic_intent"]["decision"]
    assert decision["status"] == "READY"
    assert decision["intent"]["destination_candidates"] == ["Shanghai"]
    assert decision["evidence"][0]["quote"] == "北京"
    assert len(restored.metadata["semantic_intent_history"]) == 1
    assert restored.metadata["intent_confidence"] == pytest.approx(0.95)
    # 时间类字段要按类型恢复，不能变成字符串。
    assert isinstance(restored.request.departure_after, datetime)
    assert len(restarted.events("semantic-persisted")) == event_count
    restarted.dispose()


def test_a_restored_semantic_task_still_refuses_the_legacy_route(tmp_path) -> None:
    """入口归属是任务的固有属性，重启后不能退化成默认的旧链路。"""
    repository = _new_repository(tmp_path, "entrypoint.db")
    model = ScriptedSemanticModel([semantic_decision()])
    workflow, _ = _semantic_workflow(repository, model)
    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-route"
    )
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(_database_url(tmp_path, "entrypoint.db"))
    restarted_workflow, _ = _semantic_workflow(restarted, ScriptedSemanticModel([]))

    with pytest.raises(WorkflowError, match="semantic intent entrypoint"):
        restarted_workflow.submit_message(task.task_id, "改去伦敦")
    restarted.dispose()


def test_a_semantic_conversation_continues_after_a_restart(tmp_path) -> None:
    """重启后追加一句，模型仍应收到完整对话，而不是只剩最新一句。"""
    repository = _new_repository(tmp_path, "continue.db")
    workflow, _ = _semantic_workflow(
        repository, ScriptedSemanticModel([semantic_decision()])
    )
    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-continue"
    )
    assert task.state is TaskState.WAITING_FOR_USER
    repository.dispose()

    revision = semantic_decision(
        evidence=[
            # 改口这句里仍然含"北京"，引用因此成立。
            {"turn_index": 1, "field": field, "quote": "北京"}
            for field in ("origin", "destination", "departure_after", "arrive_by")
        ]
    )
    restarted = SQLAlchemyTaskRepository(_database_url(tmp_path, "continue.db"))
    restarted_model = ScriptedSemanticModel([revision])
    restarted_workflow, _ = _semantic_workflow(restarted, restarted_model)

    resumed = restarted_workflow.submit_semantic_message(
        "semantic-continue", "还是从北京走，其他不变"
    )

    assert resumed.state is TaskState.WAITING_FOR_USER
    assert restarted_model.calls == 1
    # 重启没有截断对话：两轮用户发言都还在账本里。
    user_turns = [item for item in resumed.messages if item.role == "user"]
    assert [item.content for item in user_turns] == [
        READY_MESSAGE,
        "还是从北京走，其他不变",
    ]
    assert len(resumed.metadata["semantic_intent_history"]) == 2
    assert resumed.metadata["intent_entrypoint"] == "semantic"
    restarted.dispose()


def test_provider_retry_does_not_reinterpret_the_conversation(tmp_path) -> None:
    """Provider 抖动是外部故障，不该导致重新理解一遍用户在说什么。"""
    del tmp_path
    model = ScriptedSemanticModel([semantic_decision()])
    workflow, provider = build_demo_system(
        semantic_language_model=model,
        clock=lambda: FIXED_NOW,
        retry_sleep=lambda _: None,
    )
    provider.fail_search_remaining = 2

    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-retry"
    )

    assert task.state is TaskState.WAITING_FOR_USER
    # 模型只被问过一次；后面的重试全部发生在 Provider 层。
    assert model.calls == 1
    llm_calls = [item for item in task.tool_calls if item.tool_kind == "LLM"]
    assert len(llm_calls) == 1
    failed_searches = [
        item
        for item in task.tool_calls
        if item.tool_kind == "PROVIDER" and item.status is ToolCallStatus.FAILED
    ]
    assert failed_searches
    assert task.request is not None
    assert task.request.destination == "Shanghai"


def test_delayed_provider_retry_on_a_semantic_task_survives_a_restart(tmp_path) -> None:
    """Provider 长时间不可用时排队重试；重启后仍认得这是语义任务。"""
    now = [FIXED_NOW]
    repository = _new_repository(tmp_path, "semantic-delayed.db")
    workflow, provider = build_demo_system(
        semantic_language_model=ScriptedSemanticModel([semantic_decision()]),
        clock=lambda: now[0],
        task_repository=repository,
        retry_sleep=lambda _: None,
    )
    provider.fail_search_remaining = 3

    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-delayed"
    )
    assert task.state is TaskState.WAITING_FOR_PROVIDER
    repository.dispose()

    now[0] += timedelta(seconds=61)
    restarted = SQLAlchemyTaskRepository(_database_url(tmp_path, "semantic-delayed.db"))
    restarted_model = ScriptedSemanticModel([semantic_decision()])
    restarted_workflow, _ = build_demo_system(
        semantic_language_model=restarted_model,
        clock=lambda: now[0],
        task_repository=restarted,
        retry_sleep=lambda _: None,
    )

    assert restarted_workflow.process_due_provider_retries() == (task.task_id,)
    recovered = restarted.get(task.task_id)

    assert recovered.state is TaskState.WAITING_FOR_USER
    assert recovered.metadata["provider_retry"]["status"] == "recovered"
    assert recovered.metadata["intent_entrypoint"] == "semantic"
    # 重试补搜不重新解释：重启后的模型一次都没被调用。
    assert restarted_model.calls == 0
    restarted.dispose()


def test_an_interrupted_semantic_task_is_recovered_after_a_restart(tmp_path) -> None:
    """进程在调 Provider 时被杀掉，重启后必须显式标记失败，不能假装还在跑。"""
    repository = _new_repository(tmp_path, "semantic-interrupted.db")
    workflow, _ = _semantic_workflow(
        repository, ScriptedSemanticModel([semantic_decision()])
    )
    task = workflow.create_task_from_semantic_message(
        READY_MESSAGE, traveler_id="E1001", task_id="semantic-interrupted"
    )
    task.state = TaskState.REVALIDATING
    task.tool_calls.append(
        ToolCallRecord(
            sequence=task.tool_calls_used + 1,
            tool_name="provider.revalidate",
            tool_kind="PROVIDER",
            status=ToolCallStatus.STARTED,
            started_at=FIXED_NOW,
        )
    )
    repository.record(
        task,
        new_audit_event(
            task.task_id,
            "SIMULATED_PROCESS_INTERRUPTION",
            input_value="provider.revalidate",
            output_value="process stopped",
        ),
    )
    repository.dispose()

    restarted = SQLAlchemyTaskRepository(
        _database_url(tmp_path, "semantic-interrupted.db")
    )
    build_demo_system(
        semantic_language_model=ScriptedSemanticModel([]),
        clock=lambda: FIXED_NOW + timedelta(minutes=1),
        task_repository=restarted,
    )
    recovered = restarted.get("semantic-interrupted")

    assert recovered.state is TaskState.PROVIDER_FAILED
    assert recovered.tool_calls[-1].status is ToolCallStatus.FAILED
    assert recovered.tool_calls[-1].error_type == "InterruptedToolCall"
    assert recovered.metadata["intent_entrypoint"] == "semantic"
    restarted.dispose()
