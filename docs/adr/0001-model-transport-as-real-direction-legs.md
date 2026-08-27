# Model transport as real-direction Trip Legs

The former request model stored every route as an outbound `origin → destination` pair and reconstructed return travel by reversing it. That made “从北京回来” become `destination=Beijing`, even though Beijing is the city physically departed on the requested leg. We now record an explicit Booking Scope and project every request into real-direction Trip Legs; return-only bookings are one leg, while round trips are two legs. Legacy requests without a scope remain readable by deriving `ROUND_TRIP` from an existing return window.

## Consequences

LLMs extract grounded cities and dates inside a host-decided Booking Scope; they do not choose whether a city should be reversed. Provider queries consume Trip Legs directly, so no caller may independently reverse `origin` and `destination`.
