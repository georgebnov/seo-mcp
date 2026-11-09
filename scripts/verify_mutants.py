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
        "NoSpendAfterTheLimitAnswer",
        "parallel_callers",
        (("/\\ ~blocked /\\ count + 1 <= Limit", "/\\ count + 1 <= Limit"),),
        "a caller spends after the 429 of another path blocked the day",
    ),
    Code(
        "NoSpendAfterTheLimitAnswer",
        "src/seo_mcp/quota.py",
        (("if blocked or used + count > ledger.limit:", "if used + count > ledger.limit:"),),
        ("tests/test_failure_window.py::test_after_the_429_no_new_call_spends",),
        "quota.spend lets a new call spend after a 429 in flight",
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
    Model(
        "CountNeverBelowRequestsSent",
        "metered_call",
        (
            (
                "/\\ pc' = \"reserved\" /\\ count' = count + 1",
                "/\\ pc' = \"reserved\" /\\ count' = count",
            ),
        ),
        "the ledger gives a receipt and counts nothing",
    ),
    Code(
        "CountNeverBelowRequestsSent",
        "src/seo_mcp/quota.py",
        (('"used": used + count}', '"used": used}'),),
        ("tests/test_quota.py::test_a_429_of_the_export_api_blocks_the_day_with_no_false_count",),
        "quota.spend writes the old count",
    ),
    Model(
        "BrokenLedgerSendsNothing",
        "metered_call",
        (
            (
                '/\\ pc = "ledger" /\\ ledger = "olderDay"',
                '/\\ pc = "ledger" /\\ ledger \\in {"olderDay", "brokenText"}',
            ),
        ),
        "a ledger that does not parse reads as a new day",
    ),
    Code(
        "BrokenLedgerSendsNothing",
        "src/seo_mcp/quota.py",
        (("        raise Refused(_broken(ledger, file))\n", "        return 0, False\n"),),
        ("tests/test_quota.py::test_a_ledger_with_a_bad_count_fails_closed",),
        "quota._read reads a bad count as 0",
    ),
    Model(
        "BusyLedgerSendsNothing",
        "metered_call",
        (
            (
                'LockBusy == pc = "ledger" /\\ pc\' = "busy" /\\ Same',
                'LockBusy == pc = "ledger" /\\ pc\' = "busy" /\\ count\' = count + 1 /\\ sent\' = '
                "sent + 1 /\\ UNCHANGED <<api, cache, ledger, blocked, count0, sent0, blocked0, "
                "receipt, token>>",
            ),
        ),
        "a call goes out past a ledger that another process holds",
    ),
    Code(
        "BusyLedgerSendsNothing",
        "src/seo_mcp/quota.py",
        (
            (
                "if not await take_lock(lock, LOCK_WAIT_SECONDS):",
                "if not await take_lock(lock, LOCK_WAIT_SECONDS) and False:",
            ),
        ),
        ("tests/test_lock_timeout.py::test_a_stuck_ledger_refuses_and_sends_nothing",),
        "quota.locked goes on without the ledger lock",
    ),
    Model(
        "CacheHitSpendsNothing",
        "metered_call",
        (
            (
                'CacheHit == pc = "lock" /\\ cache = "today" /\\ pc\' = "hit" /\\ Same',
                'CacheHit == pc = "lock" /\\ cache = "today" /\\ pc\' = "hit" /\\ '
                "count' = count + 1 /\\ UNCHANGED <<api, cache, ledger, blocked, sent, count0, "
                "sent0, blocked0, receipt, token>>",
            ),
        ),
        "a saved answer still spends",
    ),
    Code(
        "CacheHitSpendsNothing",
        "src/seo_mcp/shared.py",
        (
            (
                '            _note(True, saved["at"], saved["expires_at"])\n',
                "            await prepare()\n",
            ),
        ),
        ("tests/test_quota.py::test_a_cached_answer_shows_the_count_and_spends_none",),
        "shared.once spends before it gives the saved answer",
    ),
    Model(
        "HitHasAGoodShape",
        "metered_call",
        (
            (
                'CacheBad == pc = "lock" /\\ cache = "badToday" /\\ pc\' = "ledger"',
                'CacheBad == pc = "lock" /\\ cache = "badToday" /\\ pc\' = "hit"',
            ),
        ),
        "a saved answer of a wrong shape serves",
    ),
    Code(
        "HitHasAGoodShape",
        "src/seo_mcp/shared.py",
        (('and good(stored["answer"])', "and True"),),
        ("tests/test_bad_answer.py::test_a_bad_answer_that_an_old_server_saved_is_not_served",),
        "shared._saved serves an answer of any shape",
    ),
    Model(
        "BadAnswerIsNotSaved",
        "metered_call",
        (
            (
                'BadShape == pc = "flight" /\\ pc\' = "badAnswer" /\\ Marked',
                'BadShape == pc = "flight" /\\ pc\' = "badAnswer" /\\ cache\' = "today" /\\ '
                "UNCHANGED <<api, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, "
                "token>>",
            ),
        ),
        "an HTTP 200 of a wrong shape is saved",
    ),
    Code(
        "BadAnswerIsNotSaved",
        "src/seo_mcp/shared.py",
        (("            if not good(reply):\n", "            if False:\n"),),
        ("tests/test_bad_answer.py::test_a_bad_answer_of_each_method_is_never_saved",),
        "shared.once saves an answer of any shape",
    ),
    Model(
        "SpentFailureKeepsTheWindow",
        "metered_call",
        (
            (
                'Mcp429 == pc = "flight" /\\ api = "mcp" /\\ pc\' = "failed" /\\ Marked',
                'Mcp429 == pc = "flight" /\\ api = "mcp" /\\ pc\' = "failed" /\\ Same',
            ),
        ),
        "a failed call that went out starts no failure window",
    ),
    Code(
        "SpentFailureKeepsTheWindow",
        "src/seo_mcp/shared.py",
        (("            if exc.sent or prepare is not _nothing:\n", "            if False:\n"),),
        ("tests/test_failure_window.py::test_a_repeat_after_the_call_ended_spends_nothing",),
        "shared.once marks no failure",
    ),
    Model(
        "FirstAnswerIsShared",
        "parallel_callers",
        (('cache\' = [cache EXCEPT ![s] = "saved"]', "cache' = cache"),),
        "the first answer is not saved, so the other caller of the key sends again",
    ),
    Code(
        "FirstAnswerIsShared",
        "src/seo_mcp/shared.py",
        (
            (
                '            quota.write_json(path, {"cached_at": at.isoformat(), "answer": '
                "scrub(reply)})\n",
                "            pass\n",
            ),
        ),
        ("tests/test_shared.py::test_parallel_identical_calls_share_1_request",),
        "shared.once does not save the answer",
    ),
)

DATES_MODEL = "get_seo_data_dates"
DATES: tuple[Mutant, ...] = (
    Model(
        "StartAfterEndSendsNothing",
        DATES_MODEL,
        (('DatesInOrder == pc = "named" /\\ start <= end', 'DatesInOrder == pc = "named"'),),
        "a start after the end goes on to the source",
    ),
    Code(
        "StartAfterEndSendsNothing",
        "src/seo_mcp/params.py",
        (("    if start > end:\n", "    if False:\n"),),
        ("tests/test_coverage.py::test_a_start_after_the_end_is_still_refused",),
        "params.period lets a start after the end through",
    ),
    Model(
        "StartAfterEndSendsNothing",
        DATES_MODEL,
        (('pc = "checked" /\\ start <= Newest /\\ pc\'', 'pc = "checked" /\\ pc\''),),
        "a start past the newest day goes out",
    ),
    Code(
        "StartAfterEndSendsNothing",
        "src/seo_mcp/providers/gsc.py",
        (("    if start > latest:\n", "    if False:\n"),),
        ("tests/test_coverage.py::test_a_start_past_the_newest_day_is_refused",),
        "gsc._newest_end sends a start past the UTC date",
    ),
    Model(
        "FutureEndMovesAndSaysSo",
        DATES_MODEL,
        (("sentEnd' = Min(end, Newest)", "sentEnd' = end"),),
        "an end past the newest day goes out as it is",
    ),
    Code(
        "FutureEndMovesAndSaysSo",
        "src/seo_mcp/providers/gsc.py",
        (("    return min(end, latest)\n", "    return end\n"),),
        ("tests/test_coverage.py::test_a_search_console_end_past_today_moves_to_today",),
        "gsc._newest_end sends an end past the UTC date",
    ),
    Model(
        "AppliedShowsTheSentEnd",
        DATES_MODEL,
        (("appliedEnd' = IF moved THEN sentEnd ELSE end", "appliedEnd' = end"),),
        "applied shows the asked end after a move",
    ),
    Code(
        "AppliedShowsTheSentEnd",
        "src/seo_mcp/core.py",
        (
            (
                'done["applied"]["end_date"] = done["date_moved"]["sent_end_date"]',
                'done["applied"]["end_date"] = done["applied"]["end_date"]',
            ),
        ),
        ("tests/test_coverage.py::test_applied_shows_the_end_date_that_went_out",),
        "core._asked keeps the asked end in applied",
    ),
    Model(
        "UnfinishedDayIsNeverZero",
        DATES_MODEL,
        (('ELSE IF start <= shownLast\' THEN "zero" ELSE "none"', 'ELSE "zero"'),),
        "no rows on only partial days give 0 totals",
    ),
    Code(
        "UnfinishedDayIsNeverZero",
        "src/seo_mcp/providers/gsc.py",
        (('    unknown = not totals and window["start"] > ', "    unknown = False and "),),
        (
            "tests/test_coverage.py::test_a_range_of_only_partial_days_with_no_rows_has_unknown_totals",
        ),
        "gsc._report gives 0 totals for only partial days",
    ),
    Model(
        "CompleteDayWithNoRowsIsZero",
        DATES_MODEL,
        (('ELSE IF start <= shownLast\' THEN "zero" ELSE "none"', 'ELSE "none"'),),
        "no rows on complete days give unknown totals",
    ),
    Code(
        "CompleteDayWithNoRowsIsZero",
        "src/seo_mcp/providers/gsc.py",
        (('    unknown = not totals and window["start"] > ', "    unknown = not totals or "),),
        ("tests/test_coverage.py::test_each_covered_source_shows_a_drop_to_a_real_0",),
        "gsc._report gives unknown totals for a real 0",
    ),
    Model(
        "PartialDaysAreMarked",
        DATES_MODEL,
        (("partial' = (sentEnd > shownLast')", "partial' = FALSE"),),
        "the days after the last complete day are not marked partial",
    ),
    Code(
        "PartialDaysAreMarked",
        "src/seo_mcp/providers/_google.py",
        (("    partial = {", "    partial = None and {"),),
        ("tests/test_coverage.py::test_a_day_past_the_last_complete_day_is_partial",),
        "_google.coverage marks no partial day",
    ),
    Model(
        "PartialDayNeverShownComplete",
        DATES_MODEL,
        (
            (
                'fetchedLast\' = IF cov = "api" THEN shownLast ELSE Last - 1',
                "fetchedLast' = Last - 1",
            ),
        ),
        "a saved report of an older API day serves under a newer day",
    ),
    Code(
        "PartialDayNeverShownComplete",
        "src/seo_mcp/providers/gsc.py",
        (
            (
                '    return known[0].isoformat() if known[1] == "api" else "estimate"',
                '    return "estimate"',
            ),
        ),
        ("tests/test_coverage.py::test_a_saved_report_of_a_partial_day_never_shows_it_complete",),
        "gsc._basis leaves the API day out of the report key",
    ),
    Model(
        "NoDocumentedFieldIsNeverOk",
        DATES_MODEL,
        (('Good(r) == r \\in {"rows", "noRows"}', "Good(r) == TRUE"),),
        "{} or other text is an ok answer",
    ),
    Code(
        "NoDocumentedFieldIsNeverOk",
        "src/seo_mcp/providers/gsc.py",
        (
            (
                'REPORT = shared.has_any("rows", "responseAggregationType", "metadata")',
                "REPORT = shared.is_object",
            ),
        ),
        (
            "tests/test_bad_answer.py::test_a_search_console_reply_of_no_known_field_is_a_failure",
            "tests/test_bad_answer.py::test_an_empty_object_is_not_an_empty_answer",
        ),
        "gsc.REPORT passes any object",
    ),
    Model(
        "DeltaNeverComparesNoData",
        DATES_MODEL,
        (
            (
                'delta\' = IF read\' = "found" /\\ first <= start THEN Shown ELSE "null"',
                "delta' = Shown",
            ),
        ),
        "a range that starts before the first day of data keeps its delta",
    ),
    Code(
        "DeltaNeverComparesNoData",
        "src/seo_mcp/core.py",
        (
            (
                '    result["delta"] = None\n    first = _found_day(sides)',
                "    first = _found_day(sides)",
            ),
        ),
        ("tests/test_collected.py::test_the_live_case_gives_no_delta_and_names_the_first_day",),
        "core._gate_delta keeps the delta of a range with no data",
    ),
    Model(
        "RealZeroKeepsTheDelta",
        DATES_MODEL,
        (("/\\ first <= start THEN Shown", "/\\ first < start THEN Shown"),),
        "a range that starts on the first day of data loses its delta",
    ),
    Code(
        "RealZeroKeepsTheDelta",
        "src/seo_mcp/core.py",
        (
            (
                "    if first is None or first <= start:\n",
                "    if first is None or first < start:\n",
            ),
        ),
        ("tests/test_collected.py::test_a_range_that_starts_on_the_first_event_keeps_the_delta",),
        "core._uncollected names a range that starts on the first day of data",
    ),
    Model(
        "AtMost1ExtraRead",
        DATES_MODEL,
        (("extra' = extra + 1", "extra' = extra + 2"),),
        "1 range sends 2 reads of the first day of data",
    ),
    Model(
        "ExtraReadOnlyForAGap",
        DATES_MODEL,
        (('NeedsRead == prov = "gsc" \\/ ~startRow', "NeedsRead == TRUE"),),
        "a GA4 day report with a row on its first day reads the first day again",
    ),
    Code(
        "ExtraReadOnlyForAGap",
        "src/seo_mcp/providers/ga4.py",
        (('    return dimension != "date" or params.start', "    return True or params.start"),),
        ("tests/test_collected.py::test_a_normal_comparison_sends_no_extra_read",),
        "ga4._gap_at_start asks for the first event each time",
    ),
)

AREAS: dict[str, tuple[Mutant, ...]] = {"quota": QUOTA, "dates": DATES}


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
