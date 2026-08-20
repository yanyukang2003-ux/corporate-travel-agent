# Corporate Travel Planning

This context describes the travel intent that the agent turns into policy-checked inventory searches.

## Language

**Lodging Requirement**:
The traveler's explicit need for agent-arranged lodging: `REQUIRED`, `NOT_REQUIRED`, or `UNSPECIFIED`. A multi-day trip alone does not determine this value; an explicit hotel proximity request means `REQUIRED`, while conditional wording remains `UNSPECIFIED`.
_Avoid_: Hotel flag, inferred overnight stay

**Hotel Proximity Preference**:
A preference for a hotel near the client or destination; when explicitly requested, it refines a required lodging search rather than acting as a standalone substitute for lodging need.
_Avoid_: Hotel requirement
