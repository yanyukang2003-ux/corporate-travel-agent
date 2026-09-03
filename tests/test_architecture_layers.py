"""分层守卫：`docs/architecture.md` §2 画的依赖方向，在这里对 `src/` 的每一条 import 逐条核对。

规则很短：下层不知道上层。`domain` 谁都不认识；`policy` / `workflow` 只认 `domain`；
`planning` 多认一个 `policy`；`providers` 和 `services` 是基础设施，互相可以用但不碰
`agent` / `api`；`agent` 用下面所有层；`api` 在最上面。

**已知的债**列在 `KNOWN_DEBTS` 里，每一条都写明由哪一步还。这张表是双向棘轮：出现表外的
新边测试失败；表里的债还清了（边消失了）测试也失败——那时把它从表里删掉，别让它烂在那里。
"""

from __future__ import annotations

import ast
from fnmatch import fnmatch
from pathlib import Path

PACKAGE = "corporate_travel_agent"
SRC = Path(__file__).resolve().parents[1] / "src" / PACKAGE

#: 每一层允许 import 的层。没列出来的层就是不允许。
ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset(),
    "workflow": frozenset({"domain"}),
    "policy": frozenset({"domain"}),
    "planning": frozenset({"domain", "policy"}),
    "providers": frozenset({"domain", "services"}),
    "services": frozenset({"domain", "planning", "policy", "providers", "workflow"}),
    "agent": frozenset({"domain", "planning", "policy", "providers", "services", "workflow"}),
    "demo": frozenset(
        {"agent", "domain", "planning", "policy", "providers", "services", "workflow"}
    ),
    "api": frozenset(
        {"agent", "demo", "domain", "planning", "policy", "providers", "services", "workflow"}
    ),
}

#: (模块 glob, 目标层) —— 现在存在、将来要还的边。改了代码就同步改这里。
KNOWN_DEBTS: dict[tuple[str, str], str] = {
    # 评测代码住在 services 里，反向依赖 agent 和 demo；第 5 步把它搬成独立的 evaluation 包。
    (f"{PACKAGE}.services.evaluation_*", "agent"): "step 5: move evaluation_* out of services",
    (f"{PACKAGE}.services.evaluation_*", "demo"): "step 5: move evaluation_* out of services",
}


def _layer(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == PACKAGE:
        return parts[1]
    return None


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                if path.name != "__init__.py":
                    base = base[:-1]
                base = base[: len(base) - node.level + 1]
                found.add(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                found.add(node.module)
    return {name for name in found if name.startswith(PACKAGE)}


def _edges() -> list[tuple[str, str, str]]:
    """(源模块, 源层, 目标层) 的全部跨层边。"""
    edges: list[tuple[str, str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        module = _module_name(path)
        source_layer = _layer(module)
        if source_layer is None:
            continue
        for target in _imports(path, module):
            target_layer = _layer(target)
            if target_layer and target_layer != source_layer:
                edges.append((module, source_layer, target_layer))
    return edges


def _is_known_debt(module: str, target_layer: str) -> bool:
    return any(
        fnmatch(module, pattern) and layer == target_layer for pattern, layer in KNOWN_DEBTS
    )


def test_every_layer_is_declared() -> None:
    layers = {_layer(_module_name(p)) for p in SRC.rglob("*.py")} - {None}
    undeclared = sorted(layers - ALLOWED.keys())
    assert not undeclared, f"new top-level layer(s) without a rule: {undeclared}"


def test_dependencies_only_point_downward() -> None:
    violations = sorted(
        f"{module} -> {target_layer}"
        for module, source_layer, target_layer in _edges()
        if target_layer not in ALLOWED[source_layer] and not _is_known_debt(module, target_layer)
    )
    assert not violations, "layering violations (see docs/architecture.md §2):\n  " + "\n  ".join(
        violations
    )


def test_known_debts_are_still_real() -> None:
    """债还清了就把它从表里删掉，免得表里躺着不存在的边。"""
    present = {
        (pattern, layer)
        for pattern, layer in KNOWN_DEBTS
        for module, _, target_layer in _edges()
        if fnmatch(module, pattern) and target_layer == layer
    }
    stale = sorted(
        f"{pattern} -> {layer}"
        for pattern, layer in KNOWN_DEBTS
        if (pattern, layer) not in present
    )
    assert not stale, (
        "KNOWN_DEBTS entries no longer match any import; remove them:\n  " + "\n  ".join(stale)
    )


def test_nothing_imports_the_api_layer() -> None:
    importers = sorted(
        module for module, source_layer, target_layer in _edges() if target_layer == "api"
    )
    assert not importers, f"api is the top layer; nothing may import it: {importers}"
