"""An RSA key takes tens of milliseconds to make. Only 1 fixture makes one, once per session.

The account fixtures of hundreds of tests share that key, so the suite stays fast.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parent
FIX = "Use the session fixture private_pem of tests/conftest.py, do not make a new RSA key."


def _calls(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute | ast.Name)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        == "generate_private_key"
    ]


def _session_fixture(tree: ast.Module, line: int) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "private_pem":
            continue
        if not node.lineno <= line <= (node.end_lineno or node.lineno):
            continue
        return any(ast.unparse(d) == "pytest.fixture(scope='session')" for d in node.decorator_list)
    return False


def test_only_the_session_fixture_makes_an_rsa_key():
    found = []
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for line in _calls(tree):
            if not (path.name == "conftest.py" and _session_fixture(tree, line)):
                found.append(f"{path.name}:{line}")
    assert not found, f"{', '.join(found)}: {FIX}"
