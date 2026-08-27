import unittest

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.providers.replay import ReplayProvider


class ProviderContractTests(unittest.TestCase):
    def test_replay_returns_the_exact_recorded_snapshot(self) -> None:
        _, mock = build_demo_system(clock=lambda: DEMO_CLOCK)
        request = make_demo_request(task_id="provider-contract")
        transport_query = TransportSearchQuery(
            request.origin,
            request.destination,
            request.departure_after,
            request.arrive_by,
        )
        hotel_query = HotelSearchQuery(
            request.destination,
            request.hotel_check_in,
            request.hotel_check_out,
        )
        transport_snapshot = mock.search_transport(transport_query)
        hotel_snapshot = mock.search_hotels(hotel_query)
        replay = ReplayProvider([transport_snapshot, hotel_snapshot])

        self.assertIs(replay.search_transport(transport_query), transport_snapshot)
        self.assertIs(replay.search_hotels(hotel_query), hotel_snapshot)

    def test_missing_replay_query_is_a_provider_failure_not_empty_inventory(self) -> None:
        replay = ReplayProvider([])
        request = make_demo_request(task_id="missing-replay")
        query = TransportSearchQuery(
            request.origin,
            request.destination,
            request.departure_after,
            request.arrive_by,
        )

        with self.assertRaises(ProviderError):
            replay.search_transport(query)


if __name__ == "__main__":
    unittest.main()
