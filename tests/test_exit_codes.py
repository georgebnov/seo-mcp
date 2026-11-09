"""A script or module never runs a command and then drops its exit code."""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
RUNS = {"run", "call", "Popen"}


def dropped_runs(source: str) -> list[int]:
    """Lines of a subprocess call that is a bare statement and has no check=True."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        name = call.func
        if not (
            isinstance(name, ast.Attribute)
            and name.attr in RUNS
            and isinstance(name.value, ast.Name)
            and name.value.id == "subprocess"
        ):
            continue
        checked = any(
            word.arg == "check" and isinstance(word.value, ast.Constant) and word.value.value
            for word in call.keywords
        )
        if not checked:
            found.append(node.lineno)
    return found


def test_each_command_exit_code_is_read():
    """A dropped exit code let check_models.py read the report of another run as its own."""
    problems = [
        f"{path.relative_to(ROOT)}:{line}: keep the result of subprocess.run and read its "
        "returncode, or pass check=True."
        for folder in ("scripts", "src", "tests")
        for path in sorted((ROOT / folder).rglob("*.py"))
        for line in dropped_runs(path.read_text(encoding="utf-8"))
    ]
    assert not problems, "\n".join(problems)


def test_the_scan_finds_a_dropped_exit_code():
    assert dropped_runs("import subprocess\nsubprocess.run(['x'], check=False)\n") == [2]
    assert dropped_runs("import subprocess\ndone = subprocess.run(['x'], check=False)\n") == []
    assert dropped_runs("import subprocess\nsubprocess.run(['x'], check=True)\n") == []
