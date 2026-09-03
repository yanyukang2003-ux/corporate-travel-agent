# Machine-check the layering and the port contracts

## Status

Accepted, 2026-09-03.

## Context

`docs/architecture.md` §2 draws the dependency direction between layers, and ADR-0004 states that
every orchestrator mixin reads and writes state that only `__init__` defines. Neither rule was
checked by a machine. The 2026-09-02 architecture review found the consequences: the evaluation
modules inside `services/` import `agent`, the `providers` layer imports `services` while the
diagram says it only imports `domain`, and the repository port (`TaskRepository`) had drifted from
its SQL implementation, with 22 `getattr` probes papering over the difference. Because no type
checker ran, a Protocol was documentation, not a contract; a call to a method the employee
directory does not have (`employees.get`) sat on a fallback path unnoticed.

## Decision

1. **A layering test** (`tests/test_architecture_layers.py`) parses every import under `src/` and
   asserts the allowed edges of the diagram. Known debts are listed in the test with the step that
   repays them; the test fails both on a new violation and on a debt that has been repaid but not
   removed from the list.
2. **mypy runs in CI** (`pyproject.toml` `[tool.mypy]`): default mode over all of `src/`, strict
   mode for `domain`, `workflow`, `policy`, `services/repositories.py` and `agent/orchestrator`.
   The list of strict modules is meant to grow.
3. **The orchestrator's state contract is code.** `agent/orchestrator/state.py` declares every
   instance attribute with its type and every method one mixin calls on another, as
   `NotImplementedError` stubs that the real implementations override. The cross-mixin method
   table is the coupling surface between the modules; making a mixin independent means removing
   its entries.
4. **Optional provider capabilities are `runtime_checkable` Protocols**
   (`providers/base.py` `MultiCityInventoryProvider`) rather than `hasattr` probes, so the type
   checker knows the method exists after the check.

## Consequences

- The diagram in `docs/architecture.md` §2 now shows `providers → services`, which was always
  true.
- Type errors found and fixed while enabling the checker: the `employees.get` call on the
  planning fallback path (the directory has `snapshot`), a variable reused across two loops with
  different element types in the dataset loader, and a lambda-with-default closure that is now a
  `functools.partial`.
- Adding an instance attribute to the orchestrator now requires declaring it in `state.py`;
  calling a method across mixins requires adding it to the contract table. Both are deliberate
  friction.
