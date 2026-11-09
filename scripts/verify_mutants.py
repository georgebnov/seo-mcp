"""Break each verified rule on purpose, in a copy, and check that the model or the tests catch it.

A model mutant changes the text of 1 model in a copy of models/ and runs TLC through the
statechart command: it is caught when its rule breaks. A code mutant changes 1 file in a copy of
src/ and tests/ and runs the named tests on that copy: it is caught when a test fails. The repo
and the normal cache folder stay unchanged. Each mutant first runs on the unchanged copy: its
rule must hold, or its tests must pass. Anything else is an error, not a catch.

Run: uv run python scripts/verify_mutants.py AREA, with AREA from AREAS. It needs the statechart
command, as `just models` does.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from check_models import ROOT, report_breaks, run_model


class Model(NamedTuple):
    rule: str
    model: str
    edits: tuple[tuple[str, str], ...]
    why: str


class Code(NamedTuple):
    rule: str
    file: str
    edits: tuple[tuple[str, str], ...]
    tests: tuple[str, ...]
    why: str


Mutant = Model | Code
LOG_LINES = 15
NOT_COPIED = (".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")

QUOTA: tuple[Mutant, ...] = (
    Model(
        "NeverPastTheLimit",
        "metered_call",
        (("~blocked /\\ count + 1 <= Limit", "~blocked /\\ count <= Limit"),),
        "the ledger lets 1 call past the limit",
    ),
    Code(
        "NeverPastTheLimit",
        "src/seo_mcp/quota.py",
        (("used + count > ledger.limit:", "used + count > ledger.limit + 1:"),),
        ("tests/test_quota.py::test_each_metered_method_refuses_at_the_limit",),
        "quota.spend lets 1 call past the limit",
    ),
    Model(
        "BlockedDaySendsNothing",
        "metered_call",
        (
            (
                'ledger = "good" /\\ ~blocked /\\ count + 1 <= Limit',
                'ledger = "good" /\\ count + 1 <= Limit',
            ),
        ),
        "the ledger spends on a day that the API blocked",
    ),
    Code(
        "BlockedDaySendsNothing",
        "src/seo_mcp/quota.py",
        (("if blocked or used + count > ledger.limit:", "if used + count > ledger.limit:"),),
        ("tests/test_quota.py::test_a_blocked_day_gives_the_used_count_and_the_limit",),
        "quota.spend ignores the blocked flag",
    ),
    Model(
        "Limit429SetsTheFlag",
        "metered_call",
        (
            (
                "pc' = \"dayBlocked\" /\\ blocked' = TRUE",
                "pc' = \"dayBlocked\" /\\ blocked' = blocked",
            ),
        ),
        "a 429 of the Data Export API does not block the day",
    ),
    Code(
        "Limit429SetsTheFlag",
        "src/seo_mcp/providers/clarity.py",
        (("if daily_429 and exc.status == 429:", "if daily_429 and exc.status == 430:"),),
        ("tests/test_quota.py::test_a_429_of_the_export_api_blocks_the_day_with_no_false_count",),
        "clarity._cached does not call quota.block on a 429",
    ),
    Model(
        "ReservedBeforeTheNetwork",
        "metered_call",
        (('/\\ pc = "reserved" /\\ receipt', '/\\ pc \\in {"reserved", "ledger"}'),),
        "the request goes out before the ledger counts it",
    ),
    Code(
        "ReservedBeforeTheNetwork",
        "src/seo_mcp/net.py",
        (
            (
                "    if spent is None or spent.ledger != endpoint.metered or spent.calls < 1:",
                "    if False:",
            ),
        ),
        ("tests/test_quota.py::test_a_metered_endpoint_needs_a_receipt",),
        "net._pay sends a metered call with no receipt",
    ),
    Model(
        "ConnectionFailureKeepsTheWindow",
        "metered_call",
        (('pc\' = "notSent" /\\ Marked', 'pc\' = "notSent" /\\ Same'),),
        "a spent call that did not connect starts no failure window",
    ),
    Code(
        "ConnectionFailureKeepsTheWindow",
        "src/seo_mcp/shared.py",
        (("if exc.sent or prepare is not _nothing:", "if exc.sent:"),),
        ("tests/test_failure_window.py::test_a_repeat_of_a_connection_failure_spends_nothing",),
        "shared.once marks no failure for a spent call that did not connect",
    ),
    Model(
        "TwoCallersSpendOneCall",
        "parallel_callers",
        # The anchors end in "~blocked": only TakeLockSpend has that line.
        (('cache[s] = "cold" /\\ ~failed[s]\n  /\\ ~blocked', "~failed[s]\n  /\\ ~blocked"),),
        "a caller spends again after the answer of its key is saved",
    ),
    Code(
        "TwoCallersSpendOneCall",
        "src/seo_mcp/shared.py",
        (("        if saved:\n", "        if saved and False:\n"),),
        ("tests/test_shared.py::test_parallel_identical_calls_share_1_request",),
        "shared.once does not read the saved answer under the lock",
    ),
    Model(
        "RepeatInWindowSpendsNothing",
        "parallel_callers",
        (
            (
                'cache[s] = "cold" /\\ ~failed[s]\n  /\\ ~blocked',
                'cache[s] = "cold"\n  /\\ ~blocked',
            ),
        ),
        "a caller spends again inside the failure window of its key",
    ),
    Code(
        "RepeatInWindowSpendsNothing",
        "src/seo_mcp/shared.py",
        (("        if failure:\n", "        if failure and False:\n"),),
        ("tests/test_failure_window.py::test_a_waiter_shares_the_failure_and_spends_nothing",),
        "shared.once does not read the failure window under the lock",
    ),
    Model(
        "NoAnswerOfAnotherKey",
        "parallel_callers",
        (("Slot(c) == key[c]", 'Slot(c) == "A"'),),
        "the cache path does not hold the identity of the key",
    ),
    Code(
        "NoAnswerOfAnotherKey",
        "src/seo_mcp/shared.py",
        (
            (
                "[KEY_FORMAT, site.host, _checked(auth).digest, *parts]",
                "[KEY_FORMAT, site.host, *parts]",
            ),
        ),
        ("tests/test_credential_key.py::test_2_credentials_of_1_request_send_2_times",),
        "shared.entry leaves the identity out of the cache key",
    ),
    Model(
        "NoTokenReadsNoAnswer",
        "metered_call",
        (
            ('TokenMissing == pc = "start" /\\ ~token', 'TokenMissing == pc = "ledger" /\\ ~token'),
            ('TokenRead == pc = "start" /\\ token', 'TokenRead == pc = "start"'),
        ),
        "the token is read after the saved answer, as before #49",
    ),
)

AREAS: dict[str, tuple[Mutant, ...]] = {"quota": QUOTA}


def _edit(path: Path, edits: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        found = text.count(old)
        if found != 1:
            raise LookupError(
                f"{path.name}: the mutant text is found {found} times, not 1: {old!r}"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _model(mutant: Model, work: Path) -> str:
    folder = work / "models"
    shutil.copytree(ROOT / "models", folder)
    base = report_breaks(mutant.model, run_model(mutant.model, work / "base", folder))
    if (mutant.model, mutant.rule) in base:
        return f"BROKEN: {mutant.rule} breaks in the model with no mutant"
    _edit(folder / f"{mutant.model}.tla", mutant.edits)
    report = run_model(mutant.model, work / "out", folder)
    if "## Rules that hold" not in report:
        return "error: TLC did not finish on the mutant. See the report."
    return (
        "caught" if (mutant.model, mutant.rule) in report_breaks(mutant.model, report) else "MISSED"
    )


def _pytest(work: Path, tests: tuple[str, ...]) -> tuple[int, str]:
    """Run the tests on the copy. Give the exit code and the last lines of the output."""
    env = {**os.environ, "PYTHONPATH": str(work / "src"), "SEO_MCP_CACHE_DIR": str(work / "cache")}
    command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]
    done = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, check=False)
    return done.returncode, "\n".join(done.stdout.splitlines()[-LOG_LINES:])


def _code(mutant: Code, work: Path) -> str:
    # Some tests read README.md, models/, or scripts/, so the copy holds the whole tree.
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(*NOT_COPIED), dirs_exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(work / "src")}
    probe = [sys.executable, "-c", "import seo_mcp; print(seo_mcp.__file__)"]
    where = subprocess.run(probe, env=env, capture_output=True, text=True, check=True).stdout
    if not Path(where.strip()).is_relative_to(work):
        return f"error: the tests import seo_mcp from {where.strip()}, not from the copy"
    code, log = _pytest(work, mutant.tests)
    if code != 0:
        return f"BROKEN: the code fails the tests of this rule with no mutant (exit {code})\n{log}"
    _edit(work / mutant.file, mutant.edits)
    # pytest exit 1 is a failed test. 2 to 5 are an error, a stop, or no test run.
    code, log = _pytest(work, mutant.tests)
    return "caught" if code == 1 else "MISSED" if code == 0 else f"error: pytest exit {code}\n{log}"


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in AREAS:
        print(f"Usage: verify_mutants.py AREA. AREA is 1 of: {', '.join(AREAS)}.")
        return 2
    failed = 0
    for mutant in AREAS[sys.argv[1]]:
        where = mutant.model if isinstance(mutant, Model) else mutant.file
        with tempfile.TemporaryDirectory() as folder:
            try:
                work = Path(folder)
                result = _model(mutant, work) if isinstance(mutant, Model) else _code(mutant, work)
            except LookupError as exc:
                result = f"error: {exc} Copy the new code line into {Path(__file__).name}."
        failed += result != "caught"
        head, _, log = result.partition("\n")
        print(f"{head}: {mutant.rule} in {where}: {mutant.why}")
        if log:
            print(log)
    print("Each mutant is caught." if not failed else f"{failed} lines are not caught. Read them.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
