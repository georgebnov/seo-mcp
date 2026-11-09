"""George: "No time no nothing so nothing can break with the time zone changes. And todays date
- 1 is the day it should start counting from."

Each date rule uses 1 day, the local date of the machine (params.today), read 1 time for each
call. The newest day of each dated source is that day minus 1. The tests fix the local date
and the UTC time apart, so a rule that still reads the UTC date fails.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from seo_mcp import core, params, shared
from seo_mcp.providers import clarity, ga4, gsc
from test_query_api import HOST

# The D5 case: 06:11 UTC on 2026-11-09. The local date of the machine is 2026-11-09 too.
TODAY = date(2026, 11, 9)
YESTERDAY = "2026-11-08"
MOMENT = datetime(2026, 11, 9, 6, 11, tzinfo=UTC)
DATED = ["search_queries", "ga4_organic"]


def freeze(monkeypatch, day=TODAY, moment=MOMENT):
    """Fix the local date (params._clock) and the instant of the cache times apart."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz or UTC)

    for module in (shared, gsc, ga4, clarity, params):
        monkeypatch.setattr(module, "datetime", Frozen, raising=False)
    monkeypatch.setattr(params, "_clock", lambda: day, raising=False)


def google(calls):
    return [
        json.loads(call.content)
        for call in calls
        if call.url.host in (gsc.HOST, ga4.HOST) and call.content
    ]


def reports(calls):
    """The bodies of the dated reports: no coverage read and no first-event read."""
    found = []
    for body in google(calls):
        if body.get("dataState") == "all" and body.get("dimensions") == ["date"]:
            continue
        found.append(body)
    return found


def ends(calls):
    found = set()
    for body in reports(calls):
        found |= {item["endDate"] for item in body.get("dateRanges", [])}
        found |= {body["endDate"]} if "endDate" in body else set()
    return found


async def dated(**asked):
    return (await core.get_seo_data(HOST, DATED, **asked))["sources"]


async def test_the_newest_day_of_each_dated_source_is_today_minus_1(live, monkeypatch):
    freeze(monkeypatch)
    sources = await dated(start_date="2026-11-01", end_date="2026-11-20")
    for name in DATED:
        result = sources[name]
        assert result["status"] == "ok", (name, result["reason"])
        assert result["coverage"]["end_date"] == YESTERDAY, name
        assert result["date_moved"] == {
            "asked_end_date": "2026-11-20",
            "sent_end_date": YESTERDAY,
        }, name
        assert result["applied"]["end_date"] == YESTERDAY, name
    assert ends(live) == {YESTERDAY}


async def test_an_end_of_today_moves_to_yesterday(live, monkeypatch):
    freeze(monkeypatch)
    sources = await dated(start_date="2026-11-01", end_date=TODAY.isoformat())
    for name in DATED:
        assert sources[name]["date_moved"]["sent_end_date"] == YESTERDAY, name
        assert sources[name]["applied"]["end_date"] == YESTERDAY, name
    assert ends(live) == {YESTERDAY}


async def test_an_end_of_yesterday_goes_out_as_it_is(live, monkeypatch):
    freeze(monkeypatch)
    sources = await dated(start_date="2026-11-01", end_date=YESTERDAY)
    for name in DATED:
        assert "date_moved" not in sources[name], name
        assert sources[name]["applied"]["end_date"] == YESTERDAY, name


@pytest.mark.parametrize("start", [TODAY.isoformat(), "2026-11-10"], ids=["today", "D5"])
async def test_a_start_of_today_or_later_is_refused_with_nothing_sent(live, monkeypatch, start):
    """D5: at 06:11 UTC a GA4 start of the next UTC date passed the check."""
    freeze(monkeypatch)
    sources = await dated(start_date=start, end_date=start)
    for name in DATED:
        assert sources[name]["status"] == "error", name
        assert YESTERDAY in sources[name]["reason"], name
    assert google(live) == []


@pytest.mark.parametrize(
    ("api", "method", "request_body"),
    [
        (
            "ga4",
            "run_report",
            {"dateRanges": [{"startDate": "2026-11-10", "endDate": "2026-11-10"}]},
        ),
        ("ga4", "run_report", {"dateRanges": [{"startDate": "today", "endDate": "today"}]}),
        (
            "search_console",
            "search_analytics",
            {"startDate": "2026-11-09", "endDate": "2026-11-09"},
        ),
    ],
    ids=["ga4-D5", "ga4-today", "search-console-today"],
)
async def test_query_api_refuses_a_start_past_the_newest_day(
    live, monkeypatch, api, method, request_body
):
    freeze(monkeypatch)
    data = await core.query_api(HOST, api, method, request_body)
    assert data["status"] == "error"
    assert YESTERDAY in data["reason"]
    assert google(live) == []


@pytest.mark.parametrize(
    ("start", "end", "sent", "moved"),
    [
        ("7daysAgo", "today", ("2026-11-02", YESTERDAY), True),
        ("7daysAgo", "yesterday", ("2026-11-02", YESTERDAY), False),
        ("30daysAgo", "3daysAgo", ("2026-10-10", "2026-11-06"), False),
        ("2026-11-01", "today", ("2026-11-01", YESTERDAY), True),
    ],
)
async def test_a_ga4_relative_date_goes_out_as_a_plain_date(
    live, monkeypatch, start, end, sent, moved
):
    """A relative date becomes a plain date of params.today, so today never goes out."""
    freeze(monkeypatch)
    request = {"dateRanges": [{"startDate": start, "endDate": end}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "ok", data["reason"]
    went = {"startDate": sent[0], "endDate": sent[1]}
    assert data["applied"]["dateRanges"] == [went]
    assert [body["dateRanges"] for body in reports(live)] == [[went]]
    assert data["coverage"][0]["end_date"] == sent[1]
    if moved:
        assert data["date_moved"] == [{"asked_end_date": end, "sent_end_date": YESTERDAY}]
    else:
        assert "date_moved" not in data


@pytest.mark.parametrize("text", ["99999999daysAgo", "3 days ago", "", 5])
async def test_a_ga4_date_that_names_no_day_is_refused_with_nothing_sent(live, monkeypatch, text):
    freeze(monkeypatch)
    request = {"dateRanges": [{"startDate": text, "endDate": "yesterday"}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "error"
    assert "YYYY-MM-DD" in data["reason"]
    assert google(live) == []


async def test_a_ga4_default_range_goes_out_as_plain_dates(live, monkeypatch):
    freeze(monkeypatch)
    result = (await core.get_seo_data(HOST, ["ga4_organic"]))["sources"]["ga4_organic"]
    assert result["window"] == {"startDate": "2026-10-10", "endDate": "2026-11-06"}
    assert result["coverage"]["last_complete_day"] == "2026-11-06"


async def test_1_call_reads_the_day_1_time(live, monkeypatch):
    """A call that runs over midnight keeps 1 day for all of its sources."""
    freeze(monkeypatch)
    days = iter([TODAY] + [TODAY + timedelta(days=1)] * 50)
    monkeypatch.setattr(params, "_clock", lambda: next(days), raising=False)
    sources = await dated(start_date="2026-11-01", end_date="2026-11-30")
    assert {sources[name]["coverage"]["end_date"] for name in DATED} == {YESTERDAY}


async def test_a_saved_report_of_yesterday_does_not_serve_today(live, monkeypatch):
    """A saved answer keeps the partial days of its own day: after midnight it goes again."""
    freeze(monkeypatch, TODAY, datetime(2026, 11, 9, 23, 58, tzinfo=UTC))
    await dated(start_date="2026-11-01", end_date="2026-11-05")
    before = len(reports(live))
    freeze(monkeypatch, TODAY + timedelta(days=1), datetime(2026, 11, 10, 0, 1, tzinfo=UTC))
    sources = await dated(start_date="2026-11-01", end_date="2026-11-05")
    assert len(reports(live)) == 2 * before
    for name in DATED:
        assert sources[name]["from_cache"] is False, name


# The guard: no date rule reads the clock or a zone outside params._clock.
SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
CLOCK_CALLS = {
    "date.today",
    "datetime.today",
    "datetime.now",
    "datetime.utcnow",
    "datetime.fromtimestamp",
    "time.time",
    "time.localtime",
    "time.gmtime",
    "timezone",
    "ZoneInfo",
}
ZONE_NAMES = {"UTC", "timezone", "tzinfo", "astimezone", "ZoneInfo"}
# Each place that may read the clock or a zone, and why it is no date rule of a dated source.
ALLOWED = {
    # The 1 day of each date rule: the local date of the machine, and only that call.
    ("params.py", "_clock", "date.today"),
    # The call ledger counts the UTC day of the Clarity and URL Inspection quotas.
    ("quota.py", "*"),
    # The 5-minute saved answer and the 60-second failure window are times, not days.
    ("shared.py", "*"),
    # Clarity resets its 10 calls at its own day boundary, so its day stays UTC.
    ("clarity.py", "utc_day"),
    # The GA4 token quota resets at midnight PST: a quota day, not a day of data.
    ("ga4.py", "RESET_ZONE"),
    ("ga4.py", "_pacific_day"),
    ("ga4.py", "_next_reset"),
    ("ga4.py", "_record"),
    # An OAuth token ends after 1 hour.
    ("_google.py", "token"),
    # Bing gives each row day as /Date(ms)/ at 00:00 UTC. This reads that instant, not the
    # clock: its UTC date is the day that Bing names.
    ("bing.py", "_clean"),
}


def _owners(tree: ast.Module) -> dict[ast.AST, str]:
    """The top-level function, class, or assignment name of each node of a module."""
    owners: dict[ast.AST, str] = {}
    for top in tree.body:
        if isinstance(top, ast.Import | ast.ImportFrom):
            continue
        if isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = top.name
        elif isinstance(top, ast.Assign) and isinstance(top.targets[0], ast.Name):
            name = top.targets[0].id
        else:
            name = "<module>"
        for node in ast.walk(top):
            owners[node] = name
    return owners


def _found(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and ast.unparse(node.func) in CLOCK_CALLS:
        return ast.unparse(node.func)
    if isinstance(node, ast.Name) and node.id in ZONE_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in ZONE_NAMES:
        return ast.unparse(node)
    if isinstance(node, ast.keyword) and node.arg == "tzinfo":
        return "tzinfo="
    return None


def clock_reads(source: Path = SOURCE) -> list[str]:
    found = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, owner in _owners(tree).items():
            text = _found(node)
            keys = {(path.name, owner), (path.name, "*"), (path.name, owner, text)}
            if text is None or keys & ALLOWED:
                continue
            line = getattr(node, "lineno", 0)
            found.append(f"{path.relative_to(source)}:{line} in {owner}: {text}")
    return sorted(set(found))


def test_no_date_rule_reads_the_clock_or_a_zone():
    wrong = clock_reads()
    fix = (
        "A date rule calls params.today() or params.newest(): the local date, read 1 time in "
        "params._clock. Do not read the clock, UTC, or a zone for a date. A time that is no "
        "date rule goes in ALLOWED in tests/test_plain_dates.py with its reason."
    )
    assert not wrong, f"{fix}\n" + "\n".join(wrong)
