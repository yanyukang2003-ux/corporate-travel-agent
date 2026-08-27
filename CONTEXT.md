# Corporate Travel Planning

This context describes the travel intent that the agent turns into policy-checked inventory searches.

## Language

**Lodging Requirement**:
The traveler's explicit need for agent-arranged lodging: `REQUIRED`, `NOT_REQUIRED`, or `UNSPECIFIED`. A multi-day trip alone does not determine this value; an explicit hotel proximity request means `REQUIRED`, while conditional wording remains `UNSPECIFIED`.
_Avoid_: Hotel flag, inferred overnight stay

**Hotel Proximity Preference**:
A preference for a hotel near the client or destination; when explicitly requested, it refines a required lodging search rather than acting as a standalone substitute for lodging need.
_Avoid_: Hotel requirement

**Booking Scope**:
The set of transport legs requested now: `OUTBOUND_ONLY`, `RETURN_ONLY`, or `ROUND_TRIP`. It describes the booking request, not the traveler's location history.
_Avoid_: Trip direction, return flag

**Trip Leg**:
A single transport movement whose origin, destination, and time window are expressed in the direction actually traveled. A round trip has two Trip Legs; a return-only booking has one.
_Avoid_: Canonical route, reversed outbound

**Semantic Intent**:
The traveler's current meaning derived from the complete conversation, including alternatives,
conditions, uncertainty and explicit corrections. It is not a provider request and must not receive
provider defaults.
_Avoid_: Slot patch, search parameters

**Search Command**:
A validated, executable projection compiled from a `READY` Semantic Intent. Compilation may reject
missing or conflicting meaning but may not guess what the traveler intended.
_Avoid_: Intent fields, model output

**Leg Origin**:
The city the traveler physically leaves on a Trip Leg. In “从北京回来”, Beijing is the Leg Origin.
_Avoid_: Home city, overall trip origin

**Leg Destination**:
The city the traveler physically reaches on a Trip Leg. For a return-only request it remains unknown until the traveler says where they are returning to.
_Avoid_: Visited city, business destination
