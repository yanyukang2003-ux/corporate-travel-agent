# Build the API with an application factory; split routers; type the responses

## Status

Accepted, 2026-09-03.

## Context

`api/main.py` was a 2,046-line module that assembled the whole system at import time: it read
environment variables, opened the database, built the orchestrator and both schedulers, and
registered 39 routes against module globals. One process could hold one configuration; a test
had to set environment variables before importing the module and then reach into module globals
(`api_main.workflow`, `api_main.auth_service`) to substitute parts. The public task document was
built by a 160-line hand-written dictionary, so the OpenAPI document carried request schemas but
no response schemas, and the six remaining `getattr` probes on ports all lived in this wiring.

## Decision

1. **`ApiSettings`** (`api/settings.py`) is a frozen dataclass read once from the environment, or
   constructed directly by tests. It validates the same ranges the old module did, with the same
   messages.
2. **`build_runtime(settings)`** (`api/runtime.py`) assembles one `ApiRuntime`: orchestrator,
   repositories, stores, dispatcher, schedulers. It constructs the orchestrator directly; the demo
   module is asked only for its frozen inventory when no external provider is configured
   (`demo.build_demo_provider`). Capability checks are `isinstance` on `EngineBound`, `Closeable`
   and `ModeReporting` instead of `getattr`.
3. **`create_app(runtime)`** (`api/app.py`) mounts routers split by resource (`health`, `auth`,
   `tasks`, `approvals`, `policy`, `trips`, `admin`) and stores the runtime on
   `app.state.runtime`; handlers receive it through one dependency. Two apps with different
   settings coexist in one process (`tests/test_api_factory.py`).
4. **Responses have Pydantic models** (`api/schemas.py`). Money that was sent as a number stays a
   number (`float` fields; Pydantic would otherwise turn `Decimal` into strings), money that was
   sent as a string stays a string. `tests/test_api_schemas.py` aligns every serializer dictionary
   with its model key for key, so a new field cannot silently disappear from the document.
   Free-form records (provenance, business metrics, task steps) stay untyped on purpose.
5. **`api/main.py` is a compatibility shim**: `app = create_app()` for uvicorn, plus `runtime`,
   `workflow`, `task_repository`, `raw_response_store`, `policy_configuration` for scripts and
   tests. Substituting a part means assigning on `runtime`, never rebinding a module name.

## Consequences

- No `getattr` probes on ports remain anywhere in `src/` outside the evaluation modules.
- Tests that swapped module globals now swap attributes on `api_main.runtime`; the webhook secret
  is read from settings at build time and set on the runtime in tests, not via `os.environ`.
- `api → demo` is still an allowed edge in the layering test, narrowed to the demo inventory.
- The frontend types already accepted both `string | number` for money; the wire format did not
  change for any field the drift test covers.
