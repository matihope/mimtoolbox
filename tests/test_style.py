"""Code conventions: every module, class and function has a docstring and full type hints."""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted([ROOT / "mimtoolbox.py", *(ROOT / "mimtoolbox").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])


def problems(path: pathlib.Path) -> list[str]:
    """Missing docstrings and type hints in one file, as 'file:line name: what' strings."""
    tree = ast.parse(path.read_text(), str(path))
    where = path.relative_to(ROOT)
    found = [] if ast.get_docstring(tree) else [f"{where}: module docstring"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not ast.get_docstring(node):
            found.append(f"{where}:{node.lineno} {node.name}: docstring")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            missing = [a.arg for a in params if a and a.annotation is None and a.arg not in ("self", "cls")]
            if missing:
                found.append(f"{where}:{node.lineno} {node.name}: type hints for {', '.join(missing)}")
            if node.returns is None:
                found.append(f"{where}:{node.lineno} {node.name}: return type")
    return found


class StyleTest(unittest.TestCase):
    """Docstrings and type hints everywhere (mypy --strict checks that the hints are right)."""

    def test_sources_found(self) -> None:
        """The file list covers the package, the entry point and the tests."""
        names = {p.name for p in SOURCES}
        self.assertTrue({"mimtoolbox.py", "core.py", "printer.py", "test_style.py"} <= names)

    def test_docstrings_and_type_hints(self) -> None:
        """No module, class or function lacks a docstring or type hints."""
        found = [p for path in SOURCES for p in problems(path)]
        self.assertEqual(found, [], "\n" + "\n".join(found))
