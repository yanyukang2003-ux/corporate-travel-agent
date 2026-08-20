from __future__ import annotations

import json
import os
from uuid import uuid4

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    task_id = f"postgres-smoke-{uuid4()}"
    first_repository = SQLAlchemyTaskRepository(database_url)
    first_repository.check_connection()
    first_repository.check_schema()
    first_workflow, _ = build_demo_system(task_repository=first_repository)
    created = first_workflow.create_task(make_demo_request(task_id=task_id))
    initial_event_count = len(first_repository.events(task_id))
    snapshot_count = len(first_repository.snapshots(task_id))
    first_repository.dispose()

    restarted_repository = SQLAlchemyTaskRepository(database_url)
    restarted_workflow, _ = build_demo_system(task_repository=restarted_repository)
    restored = restarted_repository.get(task_id)
    restored_state = restored.state.value
    option = next(
        item
        for item in restored.options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    completed = restarted_workflow.select_option(task_id, option.option_id)
    final_event_count = len(restarted_repository.events(task_id))
    restarted_repository.dispose()

    final_repository = SQLAlchemyTaskRepository(database_url)
    final = final_repository.get(task_id)
    final_repository.dispose()

    print(
        json.dumps(
            {
                "task_id": task_id,
                "created_state": created.state.value,
                "restored_state": restored_state,
                "completed_state": completed.state.value,
                "final_state_after_second_restart": final.state.value,
                "initial_event_count": initial_event_count,
                "final_event_count": final_event_count,
                "snapshot_count": snapshot_count,
                "booking_intent_persisted": final.booking_intent is not None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
