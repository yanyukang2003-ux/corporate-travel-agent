from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome


def main() -> None:
    workflow, provider = build_demo_system()
    task = workflow.create_task(make_demo_request(task_id="demo-trip"))
    print(f"state={task.state.value}")
    for index, option in enumerate(task.options, start=1):
        print(
            f"{index}. {option.option_id} cost={option.total_cost} "
            f"policy={option.policy_decision.outcome.value}"
        )
        for fact in option.explanation_facts:
            print(f"   - {fact}")

    exception = next(
        item
        for item in task.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    task = workflow.select_option(
        task.task_id,
        exception.option_id,
        business_reason="Customer meeting location requires the selected option",
    )
    print(f"after selection={task.state.value}, approval={task.approval.approval_id}")

    provider.price_overrides[exception.outbound.ref_id] = exception.outbound.price + 100
    task = workflow.decide_approval(
        task.task_id,
        approver_id="M2001",
        approved=True,
        reason="Approved for the customer visit",
    )
    print(f"after approval and simulated price increase={task.state.value}")
    print(f"audit_events={len(workflow.tasks.events(task.task_id))}")


if __name__ == "__main__":
    main()

