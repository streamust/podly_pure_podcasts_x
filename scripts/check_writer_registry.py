#!/usr/bin/env python3
"""Fail CI when the live Python writer surface lacks Rust parity coverage."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/writer_registry_coverage.json"
PYTHON_EXECUTOR = ROOT / "src/app/writer/executor.py"
RUST_WRITER = ROOT / "rust/src/writer"


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _writer_client_call(node: ast.Call) -> bool:
    target = node.func
    while isinstance(target, ast.Attribute):
        if target.attr == "writer_client":
            return True
        target = target.value
    return isinstance(target, ast.Name) and target.id == "writer_client"


def _literal_string(node: ast.expr | None) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def local_literal_action_targets(
    function: ast.FunctionDef | ast.AsyncFunctionDef, variable: str, call_line: int
) -> set[str] | None:
    """Resolve a local action selector only when every preceding write is literal."""
    targets: set[str] = set()
    for node in ast.walk(function):
        if node is function or getattr(node, "lineno", call_line) >= call_line:
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        assigned = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == variable
            for target in assigned
        ):
            continue
        value = (
            _literal_string(node.value) if not isinstance(node, ast.AugAssign) else None
        )
        if value is None:
            return None
        targets.add(value)
    return targets or None


def discover_python_surface() -> tuple[
    set[str], set[str], set[str], dict[str, set[str]]
]:
    tree = ast.parse(PYTHON_EXECUTOR.read_text(encoding="utf-8"))
    actions = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node.func) == "register_action"
        and node.args
        if (name := _literal_string(node.args[0])) is not None
    }

    called_actions: set[str] = set()
    dynamic_action_calls: set[str] = set()
    generic_callers: dict[str, set[str]] = {}
    for path in (ROOT / "src").rglob("*.py"):
        if "tests" in path.relative_to(ROOT / "src").parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source)
        except (OSError, SyntaxError) as exc:
            raise RuntimeError(
                f"cannot inspect production Python file {path}: {exc}"
            ) from exc
        relative = path.relative_to(ROOT).as_posix()

        class CallerVisitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative_path = relative_path
                self.functions: list[str] = []
                self.function_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
                self.ordinal: dict[tuple[str, str, str], int] = {}

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.function_nodes.append(node)
                self.generic_visit(node)
                self.function_nodes.pop()
                self.functions.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self.functions.append(node.name)
                self.function_nodes.append(node)
                self.generic_visit(node)
                self.function_nodes.pop()
                self.functions.pop()

            def visit_Call(self, node: ast.Call) -> None:
                if not _writer_client_call(node):
                    self.generic_visit(node)
                    return
                method = _call_name(node.func)
                if method == "action":
                    name = _literal_string(node.args[0]) if node.args else None
                    if name is None:
                        argument = node.args[0] if node.args else None
                        resolved = (
                            local_literal_action_targets(
                                self.function_nodes[-1], argument.id, node.lineno
                            )
                            if isinstance(argument, ast.Name) and self.function_nodes
                            else None
                        )
                        if resolved is None:
                            dynamic_action_calls.add(
                                f"{self.relative_path}:{node.lineno}"
                            )
                        else:
                            called_actions.update(resolved)
                    else:
                        called_actions.add(name)
                elif method in {"create", "update", "delete", "transaction"}:
                    model = _literal_string(node.args[0]) if node.args else None
                    model = model if model is not None else "<dynamic>"
                    operation = f"{method}:{model}"
                    function = self.functions[-1] if self.functions else "<module>"
                    ordinal_key = (function, method, model)
                    ordinal = self.ordinal.get(ordinal_key, 0)
                    self.ordinal[ordinal_key] = ordinal + 1
                    caller_id = (
                        f"{self.relative_path}::{function}::{operation}#{ordinal}"
                    )
                    generic_callers.setdefault(operation, set()).add(caller_id)
                self.generic_visit(node)

        CallerVisitor(relative).visit(module)
    return actions, called_actions, dynamic_action_calls, generic_callers


def discover_rust_actions(
    live_actions: set[str],
) -> tuple[set[str], set[str], set[str]]:
    actions_rs = (RUST_WRITER / "actions.rs").read_text(encoding="utf-8")
    # Keep names from the validator's action alternatives, excluding the
    # separate __test_* arm by intersecting with the live production registry.
    validator_names: set[str] = set()
    for body in re.findall(
        r"Operation::Action\s*\{\s*action,\s*\.\.\s*\}.*?matches!\s*\(\s*action\.as_str\(\),(.*?)\)",
        actions_rs,
        flags=re.DOTALL,
    ):
        validator_names.update(re.findall(r'"([a-z][a-z0-9_]*)"', body))
    validated = validator_names & live_actions

    handler_actions: set[str] = set()
    for path in RUST_WRITER.glob("*.rs"):
        source = path.read_text(encoding="utf-8")
        for array_body in re.findall(
            r"const\s+[A-Z_]*ACTIONS\s*:\s*&\[&str\]\s*=\s*&\[(.*?)\];",
            source,
            flags=re.DOTALL,
        ):
            handler_actions.update(re.findall(r'"([a-z][a-z0-9_]*)"', array_body))
    system = (RUST_WRITER / "system.rs").read_text(encoding="utf-8")
    handler_actions.update(re.findall(r'"([a-z][a-z0-9_]*)"\s*=>', system))
    # update_combined_config deliberately executes through the non-atomic
    # settings path in ActionRegistry::execute_non_atomic.
    if 'action == "update_combined_config"' in actions_rs:
        handler_actions.add("update_combined_config")
    generic_models = set(
        re.findall(
            r"Operation::Update\s*\{\s*model,.*?matches!\(model\.as_str\(\),([^)]*)\)",
            actions_rs,
            flags=re.DOTALL,
        )
    )
    generic_update_models = set(
        re.findall(r'"([A-Z][A-Za-z0-9_]*)"', " ".join(generic_models))
    )
    return validated, handler_actions, generic_update_models


def _literal_case_list(module_path: Path, name: str) -> list[dict[str, Any]]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            continue
        if node.value is None:
            continue
        parsed = ast.literal_eval(node.value)
        if not isinstance(parsed, (tuple, list)) or not all(
            isinstance(case, dict) for case in parsed
        ):
            raise RuntimeError(f"{name} must be a literal tuple/list of case mappings")
        return list(parsed)
    raise RuntimeError(f"{name} is missing from {module_path.relative_to(ROOT)}")


def _imported_case_tuples(tree: ast.Module) -> dict[str, tuple[Path, str]]:
    imported_tuples: dict[str, tuple[Path, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_path = ROOT / "src" / Path(*node.module.split("."))
            module_path = module_path.with_suffix(".py")
            for alias in node.names:
                imported_tuples[alias.asname or alias.name] = (module_path, alias.name)
    return imported_tuples


def _combined_case_names(tree: ast.Module) -> list[str]:
    combined_names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "WRITER_DIFFERENTIAL_CASES"
            for target in targets
        ):
            continue
        value = node.value
        if value is None:
            continue
        if not isinstance(value, ast.Tuple):
            raise RuntimeError("central WRITER_DIFFERENTIAL_CASES must be a tuple")
        for element in value.elts:
            if not isinstance(element, ast.Starred) or not isinstance(
                element.value, ast.Name
            ):
                raise RuntimeError(
                    "central differential case tuple must unpack imported case tuples"
                )
            combined_names.append(element.value.id)
        break
    else:
        raise RuntimeError(
            "central WRITER_DIFFERENTIAL_CASES is missing from the differential test runner"
        )
    return combined_names


def _parity_case_map() -> dict[str, dict[str, Any]]:
    runner = ROOT / "src/tests/test_writer_differential_parity.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"))
    imported_tuples = _imported_case_tuples(tree)
    combined_names = _combined_case_names(tree)
    cases: list[dict[str, Any]] = []
    for local_name in combined_names:
        imported = imported_tuples.get(local_name)
        if imported is None:
            raise RuntimeError(f"cannot resolve imported parity cases {local_name}")
        module_path, source_name = imported
        if not module_path.exists():
            raise RuntimeError(
                f"cannot resolve imported parity case module {module_path.relative_to(ROOT)}"
            )
        cases.extend(_literal_case_list(module_path, source_name))
    result: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in result:
            raise RuntimeError("differential case IDs must be unique strings")
        result[case_id] = case
    return result


def _executed_case_ids() -> set[str]:
    """Return case IDs used by the differential pytest parameterization."""
    test_root = ROOT / "src/tests"
    ids: set[str] = set()
    for path in test_root.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "test_writer_differential_parity":
                continue
            for decorator in node.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or _call_name(decorator.func) != "parametrize"
                ):
                    continue
                if any(
                    isinstance(item, ast.Name)
                    and item.id == "WRITER_DIFFERENTIAL_CASES"
                    for item in ast.walk(decorator)
                ):
                    ids.update(_parity_case_map())
    return ids


def case_mapping_failure(
    label: str,
    case_id: str | None,
    parity_cases: dict[str, dict[str, Any]],
    executed_cases: set[str],
    operation: str,
    *,
    action: str | None = None,
    model: str | None = None,
) -> str | None:
    if not case_id:
        return f"{label}: production-reachable operation has no mapped differential parity case"
    if case_id not in parity_cases or case_id not in executed_cases:
        return f"{label}: parity case {case_id!r} is not registered and executed by the differential test"
    case = parity_cases[case_id]
    if case.get("operation") != operation:
        return f"{label}: mapped parity case {case_id!r} covers a different operation"
    if action is not None and case.get("action") != action:
        return f"{label}: mapped parity case {case_id!r} covers a different action"
    if model is not None and case.get("model") != model:
        return f"{label}: mapped parity case {case_id!r} covers a different model"
    return None


def _action_coverage_failures(
    manifest: dict[str, Any],
    actions: set[str],
    called_actions: set[str],
    validated: set[str],
    handled: set[str],
    parity_cases: dict[str, dict[str, Any]],
    executed_cases: set[str],
) -> list[str]:
    failures: list[str] = []
    for action in sorted(actions):
        entry = manifest.get("actions", {}).get(action, {})
        case_ids = entry.get("parity_cases")
        if case_ids is None:
            case_id = entry.get("parity_case")
            case_ids = [case_id] if case_id is not None else []
        if not isinstance(case_ids, list) or any(
            not isinstance(case_id, str) for case_id in case_ids
        ):
            failures.append(f"{action}: parity_cases must be a list of case IDs")
            case_ids = []
        unreachable_reason = entry.get("unreachable_reason")
        if action in called_actions:
            if unreachable_reason:
                failures.append(
                    f"{action}: marked unreachable but has a production caller"
                )
            if not case_ids:
                failures.append(
                    f"{action}: production-reachable operation has no mapped differential parity case"
                )
        elif not unreachable_reason:
            failures.append(
                f"{action}: no production caller; add a concrete unreachable/removed-caller rationale"
            )
        # Keep compatibility-action tests pinned too. Otherwise these cases
        # can disappear from the differential matrix without the registry
        # gate noticing simply because production no longer calls the action.
        for case_id in case_ids:
            failure = case_mapping_failure(
                action, case_id, parity_cases, executed_cases, "action", action=action
            )
            if failure:
                failures.append(failure)
        if action not in validated:
            failures.append(f"{action}: absent from Rust production validation")
        if action not in handled:
            failures.append(f"{action}: absent from Rust action handlers")
    return failures


def _generic_operation_failures(
    manifest: dict[str, Any],
    generic_callers: dict[str, set[str]],
    rust_generic_models: set[str],
    parity_cases: dict[str, dict[str, Any]],
    executed_cases: set[str],
) -> list[str]:
    failures: list[str] = []
    generic_operations = set(generic_callers)
    declared_generic = set(manifest.get("generic_operations", {}))
    if declared_generic != generic_operations:
        failures.append(
            "manifest/live generic writer operations differ: "
            f"missing={sorted(generic_operations - declared_generic)}, extra={sorted(declared_generic - generic_operations)}"
        )
    for operation in sorted(generic_operations):
        method, model = operation.split(":", 1)
        entry = manifest.get("generic_operations", {}).get(operation, {})
        case_id = entry.get("parity_case")
        if model == "<dynamic>":
            failures.append(
                f"{operation}: production generic call has dynamic model and needs explicit inventory resolution"
            )
        elif method != "update":
            failures.append(
                f"{operation}: production generic operation lacks an implemented Rust parity path"
            )
        elif model not in rust_generic_models:
            failures.append(
                f"{operation}: model is absent from Rust generic update validation"
            )
        failure = case_mapping_failure(
            operation,
            case_id,
            parity_cases,
            executed_cases,
            method,
            model=model if model != "<dynamic>" else None,
        )
        if failure:
            failures.append(failure)
    return failures


def _generic_caller_inventory_failures(
    manifest: dict[str, Any], generic_callers: dict[str, set[str]]
) -> list[str]:
    declared_callers = set(manifest.get("generic_callers", []))
    declared_generic = set(manifest.get("generic_operations", {}))
    live_callers = set().union(*generic_callers.values()) if generic_callers else set()
    failures: list[str] = []
    if declared_callers != live_callers:
        failures.append(
            "manifest/live generic caller inventory differ: "
            f"missing={sorted(live_callers - declared_callers)}, extra={sorted(declared_callers - live_callers)}"
        )
    for operation, callers in sorted(generic_callers.items()):
        if operation not in declared_generic:
            # The operation-level drift already reports this. Keep its callers
            # visible in output so an added call cannot disappear in the noise.
            failures.append(f"{operation}: live generic callers: {sorted(callers)}")
    return failures


def check() -> list[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise RuntimeError("unsupported writer registry coverage manifest version")
    actions, called_actions, dynamic_action_calls, generic_callers = (
        discover_python_surface()
    )
    validated, handled, rust_generic_models = discover_rust_actions(actions)
    parity_cases = _parity_case_map()
    executed_cases = _executed_case_ids()
    failures: list[str] = []

    declared_actions = set(manifest.get("actions", {}))
    if declared_actions != actions:
        failures.append(
            "manifest/live Python action registry differ: "
            f"missing={sorted(actions - declared_actions)}, extra={sorted(declared_actions - actions)}"
        )
    failures.extend(
        _action_coverage_failures(
            manifest,
            actions,
            called_actions,
            validated,
            handled,
            parity_cases,
            executed_cases,
        )
    )
    if dynamic_action_calls:
        failures.append(
            f"dynamic/unresolved production writer action calls: {sorted(dynamic_action_calls)}"
        )
    if called_actions - actions:
        failures.append(
            f"production calls use unregistered Python actions: {sorted(called_actions - actions)}"
        )
    failures.extend(
        _generic_operation_failures(
            manifest,
            generic_callers,
            rust_generic_models,
            parity_cases,
            executed_cases,
        )
    )
    failures.extend(_generic_caller_inventory_failures(manifest, generic_callers))

    unknown_rust_actions = (validated | handled) - actions - {"update_combined_config"}
    if unknown_rust_actions:
        failures.append(
            f"Rust exposes actions absent from Python registry: {sorted(unknown_rust_actions)}"
        )
    return failures


def main() -> int:
    try:
        failures = check()
    except (OSError, ValueError, SyntaxError, RuntimeError) as exc:
        print(f"writer registry gate could not inspect inputs: {exc}", file=sys.stderr)
        return 2
    if failures:
        print("Writer registry parity gate FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nA historical action count is not used; coverage is checked against live source."
        )
        return 1
    print(
        "Writer registry parity gate passed: all live actions and generic operations have Rust coverage and executed differential parity cases."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
