from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _decorator_name(node: ast.expr) -> str:
    call = node if isinstance(node, ast.Call) else None
    target = call.func if call is not None else node
    if isinstance(target, ast.Attribute):
        prefix = target.value.id if isinstance(target.value, ast.Name) else ""
        return f"{prefix}.{target.attr}" if prefix else target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def test_all_astrbot_commands_have_readable_descriptions() -> None:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    command_decorators = {"filter.command", "filter.command_group", "qzone.command"}
    missing: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_decorator_name(item) in command_decorators for item in node.decorator_list):
            continue
        doc = ast.get_docstring(node) or ""
        if not doc.strip():
            missing.append(node.name)

    assert missing == []


def test_all_astrbot_filter_handlers_have_readable_descriptions() -> None:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    missing: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_decorator_name(item).startswith("filter.") for item in node.decorator_list):
            continue
        doc = ast.get_docstring(node) or ""
        if not doc.strip():
            missing.append(node.name)

    assert missing == []
