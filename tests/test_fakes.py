"""Tests fake only the network edge, net.transport. Each other replacement is an input on ALLOWED.

A fake of a source, a client method, or an ENDPOINTS list tests the fake, not the server. An
input is a value that the machine gives: the clock, the environment, the working folder, the
standard input, and a time or size limit that makes a test fast. monkeypatch.setenv, delenv,
and chdir set inputs, so this scan does not look at them.

The scan is a lint of the usual spellings in tests/: monkeypatch and setattr calls under any
name, attribute and item writes on an imported module, mutating calls on a module table, and
mock libraries. It does not follow a value through a function.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parent
FIX = (
    "Fake only net.transport (net.transport = ... or the fake_net fixture), "
    "or add the input to ALLOWED in tests/test_fakes.py and to the rule in AGENTS.md."
)
# (module, attribute) that a test can replace.
ALLOWED = {
    ("net", "transport"),
    ("params", "_clock"),
    ("sys", "stdin"),
    ("core", "SOURCE_TIMEOUT_SECONDS"),
    ("quota", "LOCK_WAIT_SECONDS"),
    ("shared", "PRUNE_BATCH"),
}
# Each module reads its own datetime. A test of the clock replaces it with a subclass of
# datetime, defined in the test file, that overrides only now().
CLOCK = "datetime"
# A table that a test can empty, so that each test starts from the state of a fresh import.
RESETS = {("_google", "_tokens")}
# The import path and the module table, so that a test imports a real script of scripts/.
IMPORTS = {("sys", "path"), ("sys", "modules")}
REPLACERS = {"setattr", "delattr", "setitem", "delitem", "syspath_prepend"}
MUTATORS = {
    "add",
    "append",
    "clear",
    "discard",
    "extend",
    "insert",
    "pop",
    "popitem",
    "remove",
    "setdefault",
    "update",
}
MOCKS = ("unittest.mock", "mock", "pytest_mock")


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "?"


def _root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _File:
    def __init__(self, tree: ast.Module) -> None:
        # The real name of each imported name: "from seo_mcp import providers as net" is
        # providers, so an alias does not pass as an allowed module.
        self.imported: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imported[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = alias.name
        self.clocks = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and [_dotted(base) for base in node.bases] == ["datetime"]
            and {item.name for item in node.body if isinstance(item, ast.FunctionDef)} == {"now"}
            and all(
                isinstance(item, ast.FunctionDef)
                or (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant))
                for item in node.body
            )
        }

    def owner(self, dotted: str) -> str:
        """The last part of the real module path of a target, with import aliases resolved."""
        root, _, rest = dotted.partition(".")
        real = f"{self.imported.get(root, root)}.{rest}" if rest else self.imported.get(root, root)
        return real.rsplit(".", 1)[-1]

    def module_root(self, node: ast.AST) -> bool:
        return _root(node) in self.imported


def _replaced(call: ast.Call, where: _File) -> str | None:
    """Say what a monkeypatch or setattr call replaces, or give None when it is an input."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in REPLACERS or (isinstance(func, ast.Name) and name not in {"setattr", "delattr"}):
        return None
    if name == "syspath_prepend" or not call.args:
        return "sys.path"
    first, rest = call.args[0], call.args[1:]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        target, _, attr = first.value.rpartition(".")
        value = rest[0] if rest else None
    else:
        target = _dotted(first)
        attr = str(rest[0].value) if rest and isinstance(rest[0], ast.Constant) else "?"
        value = rest[1] if len(rest) > 1 else None
    if name in {"setitem", "delitem"}:
        return f"{target}[{attr}]"
    if attr == CLOCK and isinstance(value, ast.Name) and value.id in where.clocks:
        return None
    if (where.owner(target), attr) in ALLOWED:
        return None
    return f"{target}.{attr}"


def _written(target: ast.AST, where: _File) -> str | None:
    """Say what an assignment or a del writes on an imported module, or give None."""
    if not where.module_root(target):
        return None
    if isinstance(target, ast.Attribute) and (where.owner(_dotted(target.value)), target.attr) in (
        ALLOWED
    ):
        return None
    table = target.value if isinstance(target, ast.Subscript) else None
    if isinstance(table, ast.Attribute) and (where.owner(_dotted(table.value)), table.attr) in (
        IMPORTS
    ):
        return None
    if isinstance(target, ast.Attribute | ast.Subscript):
        return _dotted(target.value if isinstance(target, ast.Subscript) else target)
    return None


def _changed(func: ast.AST, where: _File) -> str | None:
    """Say which module table a mutating call changes, or give None.

    The table is an attribute of an imported module (providers.SOURCES.update) or an imported
    name (SOURCES.update). A reset of RESETS can only clear the table.
    """
    if not isinstance(func, ast.Attribute) or func.attr not in MUTATORS:
        return None
    table = func.value
    if isinstance(table, ast.Name):
        if table.id not in where.imported:
            return None
        # An imported name has no module part, so no exception on a list matches it.
        key = ("", where.imported[table.id])
    elif isinstance(table, ast.Attribute) and where.module_root(table):
        key = (where.owner(_dotted(table.value)), table.attr)
    else:
        return None
    if key in IMPORTS or (key in RESETS and func.attr == "clear"):
        return None
    return _dotted(table)


def fakes(source: str, name: str = "<test>") -> list[str]:
    """Each fake in 1 test file that is not net.transport or an input on ALLOWED."""
    tree = ast.parse(source)
    where = _File(tree)
    found: list[tuple[int, str]] = []

    def add(node: ast.AST, what: str) -> None:
        line = getattr(node, "lineno", 0)
        found.append((line, f"{name}:{line} {what}. {FIX}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            modules += [alias.name for alias in node.names]
            if any(module.startswith(MOCKS) for module in modules):
                add(node, "imports a mock library")
        elif isinstance(node, ast.Attribute) and _dotted(node).startswith("unittest.mock"):
            add(node, "uses unittest.mock")
        elif isinstance(node, ast.arg) and node.arg == "mocker":
            add(node, "uses the mocker fixture")
        elif isinstance(node, ast.Call):
            func = node.func
            if (what := _replaced(node, where)) is not None:
                add(node, f"fakes {what}")
            elif (what := _changed(func, where)) is not None:
                add(node, f"changes {what}")
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete):
            if isinstance(node, ast.Assign | ast.Delete):
                targets = node.targets
            else:
                targets = [node.target]
            for target in targets:
                if (what := _written(target, where)) is not None:
                    add(node, f"fakes {what}")
    return [text for _, text in sorted(found)]


def test_each_test_fakes_only_the_transport():
    found = [
        fake
        for path in sorted(TESTS.rglob("*.py"))
        for fake in fakes(path.read_text(), str(path.relative_to(TESTS.parent)))
    ]
    assert not found, "\n".join(found)


BAD = """\
import httpx2
import unittest
from unittest import mock
from seo_mcp import net, providers
from seo_mcp import providers as transport_alias
from seo_mcp.providers import SOURCES, _google
def test_x(monkeypatch, mocker):
    monkeypatch.setattr(providers, 'SOURCES', {})
    monkeypatch.setattr(net.httpx2.AsyncClient, 'request', None)
    monkeypatch.setattr('seo_mcp.providers.pagespeed.ENDPOINTS', ())
    monkeypatch.setitem(providers.SOURCES, 'x', None)
    setattr(providers, 'APIS', {})
    httpx2.AsyncClient.send = None
    patcher = monkeypatch
    patcher.setattr(providers, 'SOURCES', {})
    monkeypatch.setattr(transport_alias, 'transport', None)
    providers.SOURCES['x'] = None
    providers.SOURCES.clear()
    unittest.mock.patch('seo_mcp.providers.SOURCES', {})
    monkeypatch.setattr(providers, 'datetime', None)
    monkeypatch.syspath_prepend('fakes')
    del providers.SOURCES['x']
    SOURCES.update({})
    _google._tokens.update({})
"""


def test_the_scan_finds_each_kind_of_fake():
    found = fakes(BAD)
    lines = sorted({int(fake.split(" ", 1)[0].split(":")[1]) for fake in found})
    assert lines == [3, 7, *range(8, 14), *range(15, 25)]
    assert all(fake.endswith(FIX) for fake in found)


GOOD = """\
import sys
from datetime import datetime
from seo_mcp import core, net, params
from seo_mcp.providers import _google
class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return None
def test_x(monkeypatch, tmp_path):
    net.transport = None
    monkeypatch.setattr(net, 'transport', None)
    monkeypatch.setattr('seo_mcp.net.transport', None)
    monkeypatch.setattr(params, '_clock', None)
    for module in (core, params):
        monkeypatch.setattr(module, 'datetime', Frozen, raising=False)
    monkeypatch.setattr(core, 'SOURCE_TIMEOUT_SECONDS', 0.1)
    monkeypatch.setattr(sys, 'stdin', None)
    monkeypatch.setenv('X', 'y')
    monkeypatch.chdir(tmp_path)
    _google._tokens.clear()
    sys.path.insert(0, 'scripts')
    sys.modules['check_models'] = None
    rows = []
    rows.append(1)
"""


def test_the_scan_lets_the_transport_and_the_inputs_pass():
    assert fakes(GOOD) == []
