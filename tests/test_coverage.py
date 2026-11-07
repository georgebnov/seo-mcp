"""Each dated result tells a real 0 from a partial day, and an hour of the day moves no range."""

import ast
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, providers, server
from seo_mcp.providers import _google, ga4, gsc
from test_query_api import HOST
from test_quota import replies

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
README = Path(__file__).parent.parent / "README.md"
PACIFIC = timezone(timedelta(hours=-7))
# 2 hours of 1 Pacific day in March, when California is at UTC-7. The UTC date changes between them.
MORNING = datetime(2026, 3, 18, 1, 0, tzinfo=PACIFIC)
NIGHT = datetime(2026, 3, 18, 23, 0, tzinfo=PACIFIC)
FIXED_END = "2026-03-15"
QUERY = "/searchAnalytics/query"


def freeze(monkeypatch, moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz or UTC)

    monkeypatch.setattr(_google, "datetime", Frozen, raising=False)
    monkeypatch.setattr(gsc, "datetime", Frozen, raising=False)
    monkeypatch.setattr(ga4, "datetime", Frozen, raising=False)


def row(day, dimensions):
    keys = [day for _ in dimensions]
    return {"keys": keys, "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}


def is_coverage_read(body):
    return body.get("dataState") == "all" and body.get("dimensions") == ["date"]


def search_console(fake_net, first_incomplete=None, days=()):
    """Answer the coverage read with the documented field, and each report with 1 row a day."""

    def reply(request):
        if not request.url.path.endswith(QUERY):
            return None
        body = json.loads(request.content)
        rows = [row(day, body.get("dimensions", [])) for day in days]
        answer = {"responseAggregationType": "byProperty", "rows": rows}
        if first_incomplete and is_coverage_read(body):
            answer["metadata"] = {"firstIncompleteDate": first_incomplete}
        return httpx2.Response(200, json=answer)

    return fake_net(replies(reply))


def analytics(fake_net, zone="America/New_York"):
    def reply(request):
        if request.url.host != ga4.HOST:
            return None
        return httpx2.Response(200, json={"metadata": {"timeZone": zone}, "rowCount": 0})

    return fake_net(replies(reply))


def bodies(calls):
    return [json.loads(call.content) for call in calls if call.url.path.endswith(QUERY)]


async def source(name, **params):
    return (await core.get_seo_data(HOST, [name], **params))["sources"][name]


# George's rule: the same query gives the same range at any hour of 1 day.


async def test_a_search_console_default_range_is_the_same_at_2_hours(live, fake_net, monkeypatch):
    search_console(fake_net, "2026-03-16")
    windows = []
    for moment in (MORNING, NIGHT):
        freeze(monkeypatch, moment)
        result = await source("search_queries")
        windows.append((result["coverage"]["start_date"], result["coverage"]["end_date"]))
    assert windows[0] == windows[1]
    assert windows[0][1] == FIXED_END


async def test_a_ga4_default_range_is_the_same_at_2_hours(live, fake_net, monkeypatch):
    calls = analytics(fake_net, "America/Los_Angeles")
    seen = []
    for moment in (MORNING, NIGHT):
        freeze(monkeypatch, moment)
        result = await source("ga4_organic")
        seen.append((result["coverage"]["start_date"], result["coverage"]["end_date"]))
    assert seen[0] == seen[1] == ("2026-02-16", FIXED_END)
    sent = json.loads([call for call in calls if call.url.host == ga4.HOST][0].content)
    assert sent["dateRanges"] == [{"startDate": "30daysAgo", "endDate": "3daysAgo"}]


async def test_last_days_gives_the_real_dates_that_went_out(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16")
    coverage = (await source("search_queries", last_days=7))["coverage"]
    assert (coverage["start_date"], coverage["end_date"]) == ("2026-03-09", FIXED_END)
    report = [body for body in bodies(calls) if not is_coverage_read(body)][0]
    assert (report["startDate"], report["endDate"]) == ("2026-03-09", FIXED_END)

    analytics(fake_net)
    coverage = (await source("ga4_organic", last_days=7))["coverage"]
    assert (coverage["start_date"], coverage["end_date"]) == ("2026-03-09", FIXED_END)


# A day up to the last complete day with no rows is a real 0.


async def test_no_rows_inside_the_complete_days_is_a_real_0(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16", days=["2026-03-02"])
    coverage = (await source("search_queries"))["coverage"]
    assert coverage["last_complete_day"] == FIXED_END
    assert coverage["last_complete_day_source"] == "api"
    assert "firstIncompleteDate" in coverage["last_complete_day_how"]
    assert coverage["partial_days"] is None


async def test_the_newest_row_does_not_decide_the_last_complete_day(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-15", days=["2026-03-01"])
    coverage = (await source("search_queries"))["coverage"]
    assert coverage["last_complete_day"] == "2026-03-14"


# A later day is partial: the data comes back, and the day is not a drop.


async def test_a_day_past_the_last_complete_day_is_partial(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16", days=["2026-03-17"])
    result = await source("search_queries", start_date="2026-03-10", end_date="2026-03-18")
    assert result["rows"], "A partial day keeps its data."
    assert result["coverage"]["partial_days"] == {"from": "2026-03-16", "to": "2026-03-18"}
    assert result["coverage"]["data_state"] == "all"
    assert all(body["dataState"] == "all" for body in bodies(calls))
    assert "partial" in result["reason"]


async def test_a_default_range_that_google_has_not_finished_is_marked(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-14")
    result = await source("search_queries")
    assert result["coverage"]["end_date"] == FIXED_END
    assert result["coverage"]["partial_days"] == {"from": "2026-03-14", "to": FIXED_END}


async def test_a_default_read_uses_final_data(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16")
    result = await source("search_queries")
    assert result["coverage"]["data_state"] == "final"
    assert all("dataState" not in body for body in bodies(calls) if not is_coverage_read(body))


# The value of the docs when one exists, and a marked estimate when it does not.


async def test_no_documented_value_gives_a_marked_estimate(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net)
    coverage = (await source("search_queries"))["coverage"]
    assert coverage["last_complete_day"] == FIXED_END
    assert coverage["last_complete_day_source"] == "estimate"


async def test_a_failed_coverage_read_keeps_the_report(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)

    def reply(request):
        if request.url.path.endswith(QUERY) and is_coverage_read(json.loads(request.content)):
            return httpx2.Response(500, json={})
        return None

    fake_net(replies(reply))
    result = await source("search_queries")
    assert result["status"] == "ok"
    assert result["coverage"]["last_complete_day_source"] == "estimate"


async def test_all_sources_share_1_coverage_read(live, fake_net):
    calls = search_console(fake_net, "2026-03-16")
    await core.get_seo_data(HOST, ["search_queries", "search_pages", "quick_wins"])
    assert len([body for body in bodies(calls) if is_coverage_read(body)]) == 1


async def search_analytics(request):
    return await core.query_api(HOST, "search_console", "search_analytics", request)


async def test_query_api_sends_1_query_and_marks_an_estimate(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net)
    data = await search_analytics({"startDate": "2026-03-10", "endDate": "2026-03-18"})
    assert len(bodies(calls)) == 1
    coverage = data["coverage"]
    assert coverage["last_complete_day"] == FIXED_END
    assert coverage["last_complete_day_source"] == "estimate"
    assert "dataState all" in coverage["last_complete_day_how"]
    assert coverage["data_state"] == "final"
    assert coverage["partial_days"] == {"from": "2026-03-16", "to": "2026-03-18"}


async def test_query_api_reads_the_documented_value_of_the_reply(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-14", days=["2026-03-17"])
    request = {
        "startDate": "2026-03-10",
        "endDate": "2026-03-18",
        "dimensions": ["date"],
        "dataState": "all",
    }
    coverage = (await search_analytics(request))["coverage"]
    assert coverage["last_complete_day"] == "2026-03-13"
    assert coverage["last_complete_day_source"] == "api"
    assert coverage["data_state"] == "all"


async def test_query_api_reads_the_first_incomplete_hour(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)

    def reply(request):
        if request.url.path.endswith(QUERY):
            metadata = {"firstIncompleteHour": "2026-03-17T04:00:00-07:00"}
            return httpx2.Response(200, json={"metadata": metadata})
        return None

    fake_net(replies(reply))
    request = {"startDate": "2026-03-10", "endDate": "2026-03-18", "dataState": "hourly_all"}
    coverage = (await search_analytics(request))["coverage"]
    assert coverage["last_complete_day"] == "2026-03-16"
    assert coverage["last_complete_day_source"] == "api"


async def test_ga4_marks_an_estimate_in_the_property_zone(live, fake_net, monkeypatch):
    freeze(monkeypatch, datetime(2026, 3, 18, 23, 0, tzinfo=PACIFIC))
    analytics(fake_net, "Asia/Tokyo")
    coverage = (await source("ga4_landing_pages"))["coverage"]
    assert coverage["time_zone"] == "Asia/Tokyo"
    assert coverage["last_complete_day"] == "2026-03-16"
    assert coverage["last_complete_day_source"] == "estimate"
    assert coverage["partial_days"] is None


async def test_a_ga4_range_with_a_partial_day_is_marked(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-03-18")
    assert result["coverage"]["partial_days"] == {"from": "2026-03-16", "to": "2026-03-18"}


async def test_ga4_query_api_marks_each_date_range(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    request = {
        "dateRanges": [
            {"startDate": "7daysAgo", "endDate": "today"},
            {"startDate": "2026-01-01", "endDate": "2026-01-31"},
        ],
        "metrics": [{"name": "sessions"}],
    }
    data = await core.query_api(HOST, "ga4", "run_report", request)
    first, second = data["coverage"]
    assert (first["start_date"], first["end_date"]) == ("2026-03-11", "2026-03-18")
    assert first["partial_days"] == {"from": "2026-03-16", "to": "2026-03-18"}
    assert second["partial_days"] is None


async def test_a_ga4_zone_that_the_host_cannot_read_is_named(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "Not/AZone")
    coverage = (await source("ga4_organic"))["coverage"]
    assert coverage["time_zone"] == "UTC"
    assert coverage["last_complete_day_source"] == "estimate"


async def test_a_host_with_no_timezone_data_still_answers(live, fake_net, monkeypatch):
    def no_data(name):
        raise ga4.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(ga4, "ZoneInfo", no_data)
    analytics(fake_net, "Asia/Tokyo")
    result = await source("ga4_organic")
    assert result["status"] == "ok"
    assert result["coverage"]["time_zone"] == "UTC"


# Rules that hold for each source.

COVERED = {"search_queries", "search_pages", "quick_wins", "ga4_organic", "ga4_landing_pages"}
# Each of these has a date parameter and no way to name its last complete day here.
NO_COVERAGE = {"clarity"}


def test_each_dated_source_shows_coverage_or_is_named():
    dated = {name for name, accepts in providers.ACCEPTS.items() if {"last_days"} & set(accepts)}
    wrong = sorted(dated - COVERED - NO_COVERAGE)
    fix = "Add coverage to these dated sources in their provider, then add them to COVERED:"
    assert not wrong, f"{fix} {wrong}"


@pytest.mark.parametrize("name", sorted(COVERED))
async def test_each_covered_source_shows_coverage(live, name):
    assert {"start_date", "end_date", "last_complete_day"} <= set((await source(name))["coverage"])


def test_search_console_reads_its_day_only_in_the_pacific_zone():
    tree = ast.parse((SOURCE / "providers" / "gsc.py").read_text(encoding="utf-8"))
    calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    wrong = [call for call in calls if call.endswith(("datetime.now", "date.today"))]
    fix = "Call _today(): the docs give a Search Console day in PST, not in UTC."
    assert not wrong, f"gsc.py: {fix} {wrong}"


@pytest.mark.parametrize(
    "text",
    [server.GET_SEO_DATA, server.QUERY_API, README.read_text(encoding="utf-8")],
    ids=["get_seo_data", "query_api", "README"],
)
def test_the_rule_of_a_real_0_is_in_the_text(text):
    flat = " ".join(text.split())
    assert "up to last_complete_day" in flat.replace("`", "")
    assert "real 0" in flat
    assert "partial" in flat
