"""Each dated result tells a real 0 from a partial day, and the hour of the day moves no result.

George's rule: the server works in days only. Each source says where its days end, and where the
server must compare a date with the current day, it uses 1 plain rule: the UTC date, with 1 day
of slack, because each place on Earth is on that day, the day before, or the day after.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, params, providers, server, shared, tasks
from seo_mcp.providers import clarity, ga4, gsc
from test_query_api import HOST, ga4_kind
from test_quota import metered_requests, replies

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
README = Path(__file__).parent.parent / "README.md"
PACIFIC = timezone(timedelta(hours=-7))
# 2 hours of 1 Pacific day in March, when California is at UTC-7. The UTC date changes between them.
MORNING = datetime(2026, 3, 18, 1, 0, tzinfo=PACIFIC)
NIGHT = datetime(2026, 3, 18, 23, 0, tzinfo=PACIFIC)
# Each 3rd hour of 2 UTC days: each zone of the world crosses its midnight in this time.
HOURS = [datetime(2026, 3, 17, tzinfo=UTC) + timedelta(hours=hour) for hour in range(0, 48, 3)]
# The UTC date of MORNING is 2026-03-18. A Search Console day ends 3 days later in PT, and the
# estimate takes 1 more day, because a PT day can be 1 day behind the UTC date.
ESTIMATE = "2026-03-14"
FIXED_END = "2026-03-15"
QUERY = "/searchAnalytics/query"
# The docs give a Search Console date "in PT time (UTC - 7:00/8:00)".
PACIFIC_TIMES = [timezone(timedelta(hours=-7)), timezone(timedelta(hours=-8))]


def freeze(monkeypatch, moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz or UTC)

    for module in (params, shared, gsc, ga4, clarity, tasks):
        monkeypatch.setattr(module, "datetime", Frozen, raising=False)


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
        body = {"metadata": {"timeZone": zone}, "rowCount": 0, **ga4_kind(request)}
        return httpx2.Response(200, json=body)

    return fake_net(replies(reply))


def bodies(calls):
    return [json.loads(call.content) for call in calls if call.url.path.endswith(QUERY)]


def reports(calls):
    return [body for body in bodies(calls) if not is_coverage_read(body)]


async def source(name, **params):
    return (await core.get_seo_data(HOST, [name], **params))["sources"][name]


def span(coverage):
    return coverage["start_date"], coverage["end_date"]


# The same query gives the same result at each hour: the days come from the source.


async def test_a_search_console_default_range_ends_at_the_api_day_at_each_hour(
    live, fake_net, monkeypatch
):
    calls = search_console(fake_net, "2026-03-16")
    seen = set()
    for moment in HOURS:
        freeze(monkeypatch, moment)
        result = await source("search_queries")
        seen.add((span(result["coverage"]), result["coverage"]["last_complete_day_source"]))
    assert seen == {(("2026-02-16", FIXED_END), "api")}
    assert {(body["startDate"], body["endDate"]) for body in reports(calls)} == {
        ("2026-02-16", FIXED_END)
    }


async def test_a_search_console_default_range_follows_the_api_and_not_the_clock(
    live, fake_net, monkeypatch
):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    first = await source("search_queries")
    # The coverage read is saved for 5 minutes, so the next value comes 1 hour later.
    freeze(monkeypatch, MORNING + timedelta(hours=1))
    search_console(fake_net, "2026-03-17")
    second = await source("search_queries")
    assert first["coverage"]["end_date"] == FIXED_END
    assert second["coverage"]["end_date"] == "2026-03-16"


async def test_a_ga4_default_range_is_the_same_at_each_hour(live, fake_net, monkeypatch):
    calls = analytics(fake_net, "America/Los_Angeles")
    seen = set()
    for moment in HOURS:
        freeze(monkeypatch, moment)
        coverage = (await source("ga4_organic"))["coverage"]
        seen.add((*span(coverage), coverage["last_complete_day"], coverage["partial_days"]))
    assert seen == {("30daysAgo", "3daysAgo", "3daysAgo", None)}
    sent = {
        json.dumps(json.loads(call.content)["dateRanges"])
        for call in calls
        if call.url.host == ga4.HOST
    }
    assert sent == {json.dumps([{"startDate": "30daysAgo", "endDate": "3daysAgo"}])}


async def test_last_days_gives_the_dates_that_went_out(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16")
    coverage = (await source("search_queries", last_days=7))["coverage"]
    assert span(coverage) == ("2026-03-09", FIXED_END)
    assert (reports(calls)[0]["startDate"], reports(calls)[0]["endDate"]) == span(coverage)

    analytics(fake_net)
    coverage = (await source("ga4_organic", last_days=7))["coverage"]
    assert span(coverage) == ("9daysAgo", "3daysAgo")


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
    assert coverage["end_date"] == "2026-03-14"


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


def clicks_on(day, clicks):
    """Answer firstIncompleteDate 2026-03-16, and give only `day` its clicks."""

    def reply(request):
        if not request.url.path.endswith(QUERY):
            return None
        body = json.loads(request.content)
        answer = {"responseAggregationType": "byProperty", "rows": []}
        if is_coverage_read(body):
            answer["metadata"] = {"firstIncompleteDate": "2026-03-16"}
        elif body["startDate"] == day:
            total = {"keys": [], "clicks": clicks, "impressions": 900, "ctr": 0.04, "position": 5}
            answer["rows"] = [{**total, "keys": ["q" for _ in body.get("dimensions", [])]}]
        return httpx2.Response(200, json=answer)

    return reply


def complete_day_has_clicks(fake_net, day, clicks):
    return fake_net(replies(clicks_on(day, clicks)))


# George's rule: an unfinished day is never a 0.


async def test_a_range_of_only_partial_days_with_no_rows_has_unknown_totals(
    live, fake_net, monkeypatch
):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source("search_queries", start_date="2026-03-17", end_date="2026-03-17")
    assert result["status"] == "ok"
    assert result["coverage"]["partial_days"] == {"from": "2026-03-17", "to": "2026-03-17"}
    assert result["totals"] is None
    assert "No day of the range is complete, so the total is unknown." in result["reason"]


async def test_a_range_of_only_partial_days_gives_no_drop_in_delta(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    complete_day_has_clicks(fake_net, "2026-03-10", 40)
    result = await source(
        "search_queries",
        start_date="2026-03-17",
        end_date="2026-03-17",
        compare_start_date="2026-03-10",
        compare_end_date="2026-03-10",
    )
    assert result["comparison"]["totals"]["clicks"] == 40
    assert result["totals"] is None
    assert result["delta"] is None


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-03-10", "2026-03-10"), ("2026-03-14", "2026-03-17")],
    ids=["complete", "complete_and_partial"],
)
async def test_a_range_with_a_complete_day_and_no_rows_is_a_real_0(
    live, fake_net, monkeypatch, start, end
):
    freeze(monkeypatch, MORNING)
    complete_day_has_clicks(fake_net, "2026-03-01", 40)
    result = await source(
        "search_queries",
        start_date=start,
        end_date=end,
        compare_start_date="2026-03-01",
        compare_end_date="2026-03-01",
    )
    assert result["totals"] == {"clicks": 0, "impressions": 0}
    assert result["delta"] == {"clicks": -40, "impressions": -900}
    assert "unknown" not in (result["reason"] or "")


def sessions_on(day, sessions):
    """Answer each GA4 report with no rows and no totals, as GA4 does, except for `day`."""

    def reply(request):
        if request.url.host != ga4.HOST:
            return None
        body: dict = {"metadata": {"timeZone": "UTC"}, **ga4_kind(request)}
        if json.loads(request.content)["dateRanges"][0]["startDate"] == day:
            values = [{"value": str(sessions)}, {"value": "30"}, {"value": "0.5"}, {"value": "2"}]
            body["metricHeaders"] = [{"name": name} for name in ga4.METRICS]
            body["rows"] = [{"dimensionValues": [{"value": "20260301"}], "metricValues": values}]
            body["totals"] = [{"metricValues": values}]
        return httpx2.Response(200, json=body)

    return reply


def ga4_day_has_sessions(fake_net, day, sessions):
    return fake_net(replies(sessions_on(day, sessions)))


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-03-10", "2026-03-10"), ("2026-03-14", "2026-03-17")],
    ids=["complete", "complete_and_partial"],
)
async def test_a_ga4_range_with_a_complete_day_and_no_rows_is_a_real_0(
    live, fake_net, monkeypatch, start, end
):
    freeze(monkeypatch, MORNING)
    ga4_day_has_sessions(fake_net, "2026-03-01", 40)
    result = await source(
        "ga4_organic",
        start_date=start,
        end_date=end,
        compare_start_date="2026-03-01",
        compare_end_date="2026-03-01",
    )
    assert result["totals"] == {"sessions": 0, "totalUsers": 0, "engagementRate": 0, "keyEvents": 0}
    assert result["delta"] == {
        "sessions": -40,
        "totalUsers": -30,
        "engagementRate": -0.5,
        "keyEvents": -2,
    }
    assert "unknown" not in (result["reason"] or "")


async def test_a_ga4_range_of_only_partial_days_with_no_rows_has_unknown_totals(
    live, fake_net, monkeypatch
):
    freeze(monkeypatch, MORNING)
    ga4_day_has_sessions(fake_net, "2026-03-01", 40)
    result = await source(
        "ga4_organic",
        start_date="2026-03-17",
        end_date="2026-03-17",
        compare_start_date="2026-03-01",
        compare_end_date="2026-03-01",
    )
    assert result["coverage"]["partial_days"] == {"from": "2026-03-17", "to": "2026-03-17"}
    assert result["comparison"]["totals"]["sessions"] == 40
    assert result["totals"] is None
    assert result["delta"] is None
    assert "No day of the range is complete, so the total is unknown." in result["reason"]


async def test_a_default_range_ends_on_the_last_complete_day_of_the_api(
    live, fake_net, monkeypatch
):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-14")
    result = await source("search_queries")
    assert result["coverage"]["end_date"] == "2026-03-13"
    assert result["coverage"]["partial_days"] is None
    assert result["coverage"]["data_state"] == "final"


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
    assert coverage["last_complete_day"] == ESTIMATE
    assert coverage["end_date"] == ESTIMATE
    assert coverage["last_complete_day_source"] == "estimate"


async def test_the_search_console_estimate_is_complete_in_pst_at_each_hour(
    live, fake_net, monkeypatch
):
    search_console(fake_net)
    for moment in HOURS:
        freeze(monkeypatch, moment)
        coverage = (await source("search_queries"))["coverage"]
        for pacific in PACIFIC_TIMES:
            newest_complete = moment.astimezone(pacific).date() - timedelta(days=gsc.LAG_DAYS)
            assert date.fromisoformat(coverage["last_complete_day"]) <= newest_complete, moment


async def test_a_saved_search_console_report_keeps_its_estimate_across_midnight(
    live, fake_net, monkeypatch
):
    calls = search_console(fake_net)
    named = {"start_date": "2026-03-10", "end_date": "2026-03-18"}
    freeze(monkeypatch, datetime(2026, 3, 18, 23, 58, tzinfo=UTC))
    first = await source("search_queries", **named)
    freeze(monkeypatch, datetime(2026, 3, 19, 0, 1, tzinfo=UTC))
    second = await source("search_queries", **named)
    assert len(reports(calls)) == 2, "The 2nd report must come from the saved answer."
    assert first["coverage"]["last_complete_day"] == ESTIMATE
    assert second["coverage"] == first["coverage"]


async def test_a_saved_report_of_a_partial_day_never_shows_it_complete(live, fake_net, monkeypatch):
    t0 = datetime(2026, 3, 18, 20, 0, tzinfo=UTC)
    named = {"start_date": "2026-03-10", "end_date": "2026-03-18"}
    freeze(monkeypatch, t0)
    search_console(fake_net, "2026-03-17", days=["2026-03-17"])
    await source("search_pages")
    freeze(monkeypatch, t0 + timedelta(seconds=200))
    first = await source("search_queries", **named)
    assert first["coverage"]["partial_days"]["from"] == "2026-03-17"
    # Google moves its day: the coverage entry ends at t0+300 s, and the report entry is still good.
    freeze(monkeypatch, t0 + timedelta(seconds=350))
    calls = search_console(fake_net, "2026-03-18", days=["2026-03-17"])
    second = await source("search_queries", **named)
    assert second["coverage"]["last_complete_day"] == "2026-03-17"
    assert len(reports(calls)) == 2, "Rows of a partial day must not serve as complete."
    assert second["from_cache"] is False


async def test_a_saved_report_serves_while_the_day_does_not_move(live, fake_net, monkeypatch):
    t0 = datetime(2026, 3, 18, 20, 0, tzinfo=UTC)
    named = {"start_date": "2026-03-10", "end_date": "2026-03-18"}
    freeze(monkeypatch, t0)
    search_console(fake_net, "2026-03-17", days=["2026-03-17"])
    first = await source("search_queries", **named)
    freeze(monkeypatch, t0 + timedelta(seconds=200))
    calls = search_console(fake_net, "2026-03-17", days=["2026-03-17"])
    second = await source("search_queries", **named)
    assert bodies(calls) == []
    assert second["from_cache"] is True
    assert second["coverage"] == first["coverage"]


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
    assert coverage["last_complete_day"] == ESTIMATE
    assert coverage["last_complete_day_source"] == "estimate"
    assert "dataState all" in coverage["last_complete_day_how"]
    assert coverage["data_state"] == "final"
    assert coverage["partial_days"] == {"from": "2026-03-15", "to": "2026-03-18"}


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


async def test_ga4_names_its_last_complete_day_in_its_own_terms(live, fake_net, monkeypatch):
    freeze(monkeypatch, NIGHT)
    analytics(fake_net, "Asia/Tokyo")
    coverage = (await source("ga4_landing_pages"))["coverage"]
    assert coverage["time_zone"] == "Asia/Tokyo"
    assert coverage["last_complete_day"] == "3daysAgo"
    assert coverage["last_complete_day_source"] == "estimate"
    assert coverage["partial_days"] is None


async def test_a_ga4_range_with_a_partial_day_is_marked(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-03-18")
    # 2026-03-15 is complete only where the day is 2026-03-18 or later, so it is partial.
    assert result["coverage"]["partial_days"] == {"from": "2026-03-15", "to": "2026-03-18"}


@pytest.mark.parametrize(
    ("start", "end", "partial"),
    [
        ("7daysAgo", "today", {"from": "2daysAgo", "to": "today"}),
        ("1daysAgo", "today", {"from": "1daysAgo", "to": "today"}),
        ("30daysAgo", "3daysAgo", None),
        ("2026-03-01", "today", {"from": "2daysAgo", "to": "today"}),
        ("2026-03-16", "yesterday", {"from": "2026-03-16", "to": "yesterday"}),
        ("10daysAgo", "2026-03-18", {"from": "2026-03-15", "to": "2026-03-18"}),
        ("4daysAgo", "2026-03-18", {"from": "4daysAgo", "to": "2026-03-18"}),
        ("2026-01-01", "2026-01-31", None),
    ],
)
async def test_ga4_query_api_marks_each_kind_of_range(
    live, fake_net, monkeypatch, start, end, partial
):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    request = {"dateRanges": [{"startDate": start, "endDate": end}], "metrics": [{"name": "x"}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    (coverage,) = data["coverage"]
    assert span(coverage) == (start, end)
    assert coverage["partial_days"] == partial


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "Pacific/Pago_Pago", "Not/AZone", None])
async def test_the_name_of_a_ga4_zone_changes_no_date(live, fake_net, monkeypatch, zone):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, zone)
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-03-18")
    assert result["status"] == "ok"
    assert span(result["coverage"]) == ("2026-03-10", "2026-03-18")
    assert result["coverage"]["partial_days"] == {"from": "2026-03-15", "to": "2026-03-18"}
    assert [
        json.loads(call.content)["dateRanges"] for call in calls if call.url.host == ga4.HOST
    ] == [[{"startDate": "2026-03-10", "endDate": "2026-03-18"}]]


async def test_a_saved_ga4_answer_keeps_its_days_across_midnight(live, fake_net, monkeypatch):
    analytics(fake_net, "UTC")
    request = {
        "dateRanges": [
            {"startDate": "2026-03-10", "endDate": "2026-03-18"},
            {"startDate": "30daysAgo", "endDate": "3daysAgo"},
        ],
        "metrics": [{"name": "sessions"}],
    }
    freeze(monkeypatch, datetime(2026, 3, 18, 23, 58, tzinfo=UTC))
    first = await core.query_api(HOST, "ga4", "run_report", request)
    freeze(monkeypatch, datetime(2026, 3, 19, 0, 1, tzinfo=UTC))
    second = await core.query_api(HOST, "ga4", "run_report", request)
    assert second["from_cache"] is True
    assert second["coverage"] == first["coverage"]


# An end past the newest day of a source moves to that day, and the result shows the move.


def sent_bodies(calls, host):
    return [json.loads(call.content) for call in calls if call.url.host == host]


async def test_a_search_console_end_past_today_moves_to_today(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16", days=["2026-03-17"])
    result = await source("search_queries", start_date="2026-03-10", end_date="2026-04-01")
    assert result["status"] == "ok"
    assert result["date_moved"] == {
        "asked_end_date": "2026-04-01",
        "sent_end_date": "2026-03-18",
    }
    assert "moved from 2026-04-01 to 2026-03-18" in result["reason"]
    reports = [body for body in bodies(calls) if not is_coverage_read(body)]
    assert {body["endDate"] for body in reports} == {"2026-03-18"}
    assert result["coverage"]["end_date"] == "2026-03-18"


def test_a_valid_search_console_day_never_moves_at_any_hour(monkeypatch):
    for moment in HOURS:
        freeze(monkeypatch, moment)
        for pacific in PACIFIC_TIMES:
            today = moment.astimezone(pacific).date()
            for start in (today - timedelta(days=5), today):
                for end in (today - timedelta(days=1), today, today + timedelta(days=2)):
                    if start > end:
                        continue
                    sent = gsc._newest_end("start_date", start, end)
                    case = (moment, pacific, start, end, sent)
                    assert sent >= min(end, today), case
                    assert sent >= start, case
                    if end <= today:
                        assert sent == end, case


async def test_an_end_inside_the_source_days_does_not_move(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source("search_queries", start_date="2026-03-10", end_date="2026-03-18")
    assert "date_moved" not in result


async def test_a_ga4_end_past_each_zone_goes_out_as_today(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, "Asia/Tokyo")
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-04-01")
    assert sent_bodies(calls, ga4.HOST)[0]["dateRanges"][0]["endDate"] == "today"
    assert result["date_moved"] == {"asked_end_date": "2026-04-01", "sent_end_date": "today"}
    assert result["coverage"]["end_date"] == "today"
    assert "2026-04-01" in result["reason"]
    assert "today" in result["reason"]


async def test_a_ga4_end_of_the_utc_day_plus_1_goes_out_as_today(live, fake_net, monkeypatch):
    # The UTC day is the 18th. In a property behind UTC the 19th has no data yet, and in a
    # property ahead of UTC the 19th is today: today is exactly the newest day in both.
    freeze(monkeypatch, datetime(2026, 3, 18, 20, 0, tzinfo=UTC))
    calls = analytics(fake_net, "America/Los_Angeles")
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-03-19")
    assert sent_bodies(calls, ga4.HOST)[0]["dateRanges"][0]["endDate"] == "today"
    assert result["date_moved"]["sent_end_date"] == "today"


@pytest.mark.parametrize("start", ["2026-03-18", "2026-03-19"])
async def test_a_ga4_move_can_never_reverse_a_range(live, fake_net, monkeypatch, start):
    # A property behind UTC can still be on the 17th, so "today" could come before this start.
    freeze(monkeypatch, datetime(2026, 3, 18, 2, 0, tzinfo=UTC))
    calls = analytics(fake_net, "America/Los_Angeles")
    await source("ga4_organic", start_date=start, end_date="2026-04-01")
    sent = sent_bodies(calls, ga4.HOST)[0]["dateRanges"]
    assert sent == [{"startDate": start, "endDate": "2026-03-19"}]


def test_a_ga4_move_keeps_each_valid_day_in_each_zone_at_each_hour(monkeypatch):
    """No zone and no hour: a sent end never cuts a day that the property has, or reverses."""
    for moment in HOURS:
        freeze(monkeypatch, moment)
        for hours in range(-12, 15):
            today = (moment + timedelta(hours=hours)).date()
            for start in (today - timedelta(days=5), today - timedelta(days=1), today):
                for end in (
                    today - timedelta(days=1),
                    today,
                    today + timedelta(days=1),
                    today + timedelta(days=4),
                ):
                    if start > end:
                        continue
                    sent = ga4._newest_end("start_date", start.isoformat(), end.isoformat())
                    real = today if sent == "today" else date.fromisoformat(sent)
                    case = (moment, hours, start, end, sent)
                    assert real >= min(end, today), case
                    assert real >= start, case
                    if end <= today:
                        assert real == end, case


async def test_query_api_moves_a_search_console_end(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net)
    data = await search_analytics({"startDate": "2026-03-10", "endDate": "2026-04-01"})
    assert bodies(calls)[0]["endDate"] == "2026-03-18"
    assert data["applied"]["endDate"] == "2026-03-18"
    assert data["date_moved"]["asked_end_date"] == "2026-04-01"
    assert "moved from 2026-04-01" in data["reason"]


async def test_query_api_moves_each_ga4_range_alone(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, "America/Los_Angeles")
    request = {
        "dateRanges": [
            {"startDate": "2026-03-01", "endDate": "2026-04-01"},
            {"startDate": "2026-02-01", "endDate": "2026-02-10"},
            {"startDate": "7daysAgo", "endDate": "yesterday"},
        ],
        "metrics": [{"name": "sessions"}],
    }
    data = await core.query_api(HOST, "ga4", "run_report", request)
    sent = sent_bodies(calls, ga4.HOST)[0]["dateRanges"]
    assert [item["endDate"] for item in sent] == ["today", "2026-02-10", "yesterday"]
    assert data["date_moved"] == [
        {"asked_end_date": "2026-04-01", "sent_end_date": "today"},
    ]


RECORDING = {
    "filters": {"date": {"start": "2026-03-10T00:00:00.000Z", "end": "2026-04-01T00:00:00.000Z"}}
}


async def test_query_api_moves_a_clarity_end_to_the_end_of_the_utc_day(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)

    def reply(request):
        if metered_requests([request]):
            return httpx2.Response(200, json=[])
        return None

    calls = fake_net(replies(reply))
    data = await core.query_api(HOST, "clarity", "recordings", RECORDING)
    latest = "2026-03-18T23:59:59.999Z"
    body = json.loads(metered_requests(calls)[0].content)
    assert body["end"] == latest
    assert body["filters"]["date"]["end"] == latest
    assert data["date_moved"]["sent_end_date"] == latest
    await core.query_api(HOST, "clarity", "recordings", RECORDING)
    assert len(metered_requests(calls)) == 1, (
        "A repeat of a moved request must be the same request."
    )


async def test_a_move_that_makes_2_ranges_unequal_is_reported(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source(
        "search_queries",
        start_date="2026-03-11",
        end_date="2026-03-30",
        compare_start_date="2026-02-01",
        compare_end_date="2026-02-20",
    )
    assert result["range_days"] == {"main": 8, "comparison": 20}
    assert "8 and 20 days" in result["reason"]
    assert "unequal" in result["reason"]
    assert "date_moved" not in result["comparison"]
    assert result["comparison"]["coverage"]["end_date"] == "2026-02-20"


async def test_a_ga4_move_to_today_gives_a_length_that_can_vary(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    result = await source(
        "ga4_organic",
        start_date="2026-03-11",
        end_date="2026-04-01",
        compare_start_date="2026-02-01",
        compare_end_date="2026-02-08",
    )
    # today is the 17th, the 18th, or the 19th, so the main range has 7 to 9 days.
    assert result["range_days"] == {"main": [7, 9], "comparison": 8}
    assert "7 to 9 and 8 days" in result["reason"]


async def test_a_move_that_keeps_2_ranges_equal_adds_no_note(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source(
        "search_queries",
        start_date="2026-03-11",
        end_date="2026-03-30",
        compare_start_date="2026-02-01",
        compare_end_date="2026-02-08",
    )
    assert "range_days" not in result


# A start past the newest day leaves no range, so nothing goes out and nothing is spent.


def sent_nothing(calls):
    assert not calls, f"The server sent {[str(call.url) for call in calls]}"


@pytest.mark.parametrize("name", ["search_queries", "ga4_organic"])
async def test_a_start_past_the_newest_day_is_refused(live, fake_net, monkeypatch, name):
    freeze(monkeypatch, MORNING)
    calls = fake_net(replies(lambda _: None))
    result = await source(name, start_date="2026-03-25", end_date="2026-04-01")
    assert result["status"] == "error"
    assert "most recent day" in result["reason"]
    assert "sent nothing" in result["reason"]
    sent_nothing(calls)


@pytest.mark.parametrize(
    ("api", "method", "request_body"),
    [
        (
            "search_console",
            "search_analytics",
            {"startDate": "2026-03-25", "endDate": "2026-04-01"},
        ),
        (
            "ga4",
            "run_report",
            {"dateRanges": [{"startDate": "2026-03-25", "endDate": "2026-04-01"}]},
        ),
        (
            "clarity",
            "recordings",
            {
                "filters": {
                    "date": {
                        "start": "2026-03-25T00:00:00.000Z",
                        "end": "2026-04-01T00:00:00.000Z",
                    }
                }
            },
        ),
    ],
    ids=["search_console", "ga4", "clarity"],
)
async def test_query_api_refuses_a_start_past_the_newest_day(
    live, fake_net, monkeypatch, api, method, request_body
):
    freeze(monkeypatch, MORNING)
    calls = fake_net(replies(lambda _: None))
    data = await core.query_api(HOST, api, method, request_body)
    assert data["status"] == "error"
    assert "most recent day" in data["reason"]
    sent_nothing(calls)


@pytest.mark.parametrize("name", ["search_queries", "ga4_organic"])
async def test_a_start_after_the_end_is_still_refused(live, fake_net, monkeypatch, name):
    freeze(monkeypatch, MORNING)
    calls = fake_net(replies(lambda _: None))
    data = await core.get_seo_data(HOST, [name], start_date="2026-03-10", end_date="2026-03-01")
    assert data["status"] == "error"
    assert "must not be after" in data["reason"]
    sent_nothing(calls)


async def test_a_ga4_start_after_the_end_in_query_api_is_refused(live, fake_net):
    calls = fake_net(replies(lambda _: None))
    request = {"dateRanges": [{"startDate": "2026-03-10", "endDate": "2026-03-01"}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "error"
    sent_nothing(calls)


# GA4 reads a relative date in the property zone, and the server knows only the UTC date U
# (2026-03-18 at MORNING). The property day is U-1, U, or U+1.
@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        ("yesterday", "3daysAgo", "fewer days ago"),
        ("today", "yesterday", "fewer days ago"),
        ("2daysAgo", "5daysAgo", "fewer days ago"),
        # yesterday is U at the latest, so a start of U+1 is after it in every zone.
        ("2026-03-19", "yesterday", "every time zone"),
        # today is U-1 at the earliest, so it is after an end of U-2 in every zone.
        ("today", "2026-03-16", "every time zone"),
    ],
)
async def test_a_ga4_relative_start_after_the_end_sends_nothing(
    live, fake_net, monkeypatch, start, end, why
):
    freeze(monkeypatch, MORNING)
    calls = fake_net(replies(lambda _: None))
    request = {"dateRanges": [{"startDate": start, "endDate": end}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "error"
    assert "dateRanges startDate must not be after the end date" in data["reason"]
    assert why in data["reason"]
    assert "sent nothing" in data["reason"]
    sent_nothing(calls)


# Each of these is a valid range in some property zone, so it goes out as asked.
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("3daysAgo", "3daysAgo"),
        ("7daysAgo", "yesterday"),
        # In a zone on U+1, yesterday is U.
        ("2026-03-18", "yesterday"),
        # In a zone on U-1, today is U-1.
        ("today", "2026-03-17"),
    ],
)
async def test_a_ga4_range_that_can_be_valid_goes_out(live, fake_net, monkeypatch, start, end):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net)
    request = {"dateRanges": [{"startDate": start, "endDate": end}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "ok", data["reason"]
    sent = [json.loads(call.content) for call in calls if call.url.host == ga4.HOST]
    assert [body["dateRanges"] for body in sent] == [[{"startDate": start, "endDate": end}]]


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
async def test_each_covered_source_shows_a_moved_end(live, monkeypatch, name):
    freeze(monkeypatch, MORNING)
    result = await source(name, start_date="2026-03-10", end_date="2026-04-01")
    assert result["status"] == "ok"
    moved = result["date_moved"]
    assert moved["asked_end_date"] == "2026-04-01"
    assert moved["sent_end_date"] == result["coverage"]["end_date"]
    assert "moved from" in result["reason"]


@pytest.mark.parametrize("name", sorted(COVERED))
async def test_no_covered_source_shows_a_0_for_only_partial_days(live, fake_net, monkeypatch, name):
    freeze(monkeypatch, MORNING)

    def no_data(request):
        if request.url.host == ga4.HOST:
            body = {"metadata": {"timeZone": "UTC"}, "rowCount": 0, **ga4_kind(request)}
            return httpx2.Response(200, json=body)
        if not request.url.path.endswith(QUERY):
            return None
        answer: dict = {"responseAggregationType": "byProperty", "rows": []}
        if is_coverage_read(json.loads(request.content)):
            answer["metadata"] = {"firstIncompleteDate": "2026-03-16"}
        return httpx2.Response(200, json=answer)

    fake_net(replies(no_data))
    result = await source(
        name,
        start_date="2026-03-17",
        end_date="2026-03-17",
        compare_start_date="2026-03-01",
        compare_end_date="2026-03-01",
    )
    assert result["coverage"]["partial_days"]["from"] <= "2026-03-17"
    zeros = sorted(key for key, value in (result["totals"] or {}).items() if value == 0)
    drops = sorted(key for key, value in (result.get("delta") or {}).items() if value < 0)
    fix = "A range of only partial days has unknown totals (None) and no delta, never a 0:"
    assert not zeros and not drops, f"{name}: {fix} {zeros} {drops}"


@pytest.mark.parametrize("name", sorted(COVERED))
async def test_each_covered_source_shows_a_drop_to_a_real_0(live, fake_net, monkeypatch, name):
    freeze(monkeypatch, MORNING)
    ga4_reply, gsc_reply = sessions_on("2026-03-01", 40), clicks_on("2026-03-01", 40)
    fake_net(replies(lambda request: ga4_reply(request) or gsc_reply(request)))
    result = await source(
        name,
        start_date="2026-03-10",
        end_date="2026-03-10",
        compare_start_date="2026-03-01",
        compare_end_date="2026-03-01",
    )
    totals, delta = result["totals"] or {}, result.get("delta") or {}
    fix = "A complete range with no rows has totals 0 for each metric, and delta shows the drop:"
    shown = bool(totals) and not any(totals.values()) and any(value < 0 for value in delta.values())
    assert shown, f"{name}: {fix} totals {result['totals']}, delta {result.get('delta')}"


@pytest.mark.parametrize("name", sorted(COVERED))
async def test_each_covered_source_shows_coverage(live, name):
    assert {"start_date", "end_date", "last_complete_day"} <= set((await source(name))["coverage"])


# Each clock read that gives a day. params.today is the 1 rule for the day of a request. The
# ledger day of quota.py and the GA4 quota day (the docs: a reset at midnight PST) are no request.
DAY_READS = {("params.py", "UTC"), ("quota.py", "UTC"), ("ga4.py", "RESET_ZONE")}
OFFSETS = {"ga4.py"}


def day_reads():
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name.endswith(("date.today", "today_in")):
                yield path.name, name
            if name == "timezone" and path.name not in OFFSETS:
                yield path.name, ast.unparse(node)
            inner = node.func.value if isinstance(node.func, ast.Attribute) else None
            is_day = name.endswith(".date") and isinstance(inner, ast.Call)
            if is_day and ast.unparse(inner.func).endswith("now"):
                zone = ast.unparse(inner.args[0]) if inner.args else ""
                if (path.name, zone) not in DAY_READS:
                    yield path.name, ast.unparse(node)


def test_only_params_gives_the_day_of_a_request():
    wrong = sorted(set(day_reads()))
    fix = (
        "Call params.today(): it is the 1 day rule of the server, the UTC date with no zone. "
        "Do not read a zone or an offset for the day of a request."
    )
    assert not wrong, f"{fix} {wrong}"


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
    assert "date_moved" in flat
