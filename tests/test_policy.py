import unittest
from decimal import Decimal

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome


class PolicyEvidenceTests(unittest.TestCase):
    def test_every_decision_has_versioned_rule_evidence(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="trip-policy-evidence"))
        active_version = workflow.policies.current().policy_version

        for option in task.options:
            self.assertTrue(option.policy_decision.evidence)
            for evidence in option.policy_decision.evidence:
                self.assertTrue(evidence.rule_id)
                self.assertEqual(evidence.policy_version, active_version)
                self.assertTrue(evidence.actual)
                self.assertTrue(evidence.threshold)

    def test_hotel_over_cap_is_not_silently_treated_as_compliant(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="trip-hotel-cap"))
        near_hotel_option = next(item for item in task.options if item.hotel.ref_id == "HT-NEAR")

        hotel_evidence = next(
            item
            for item in near_hotel_option.policy_decision.evidence
            if item.rule_id == "hotel.city.nightly_cap"
        )
        self.assertEqual(hotel_evidence.actual, str(Decimal("720")))
        self.assertEqual(hotel_evidence.threshold, str(Decimal("600")))
        self.assertEqual(hotel_evidence.outcome, PolicyOutcome.REQUIRES_APPROVAL)


if __name__ == "__main__":
    unittest.main()

