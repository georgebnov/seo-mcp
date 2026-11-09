"""Each dated result tells a real 0 from a partial day, and the hour of the day moves no result.

George's rule: "No time at all, everything works on dates." Each source says where its days end.
Where the server compares a date with the current day, it uses 1 day: params.today, the local
date of the machine. The newest day of each dated source is that day minus 1.
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
# The local date of MORNING is 2026-03-18, so the newest day is 2026-03-17. The Search Console
# estimate is 3 days before the newest day.
NEWEST = "2026-03-17"
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
    # The local date of the machine is the date of moment in its own zone.
    monkeypatch.setattr(params, "_clock", moment.date)


def row(day, dimensions):
    keys = [day for _ in dimensions]
    return {"keys": keys, "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}


def is_coverage_read(body):
    return body.get("dataState") == "all" and body.get("dimensions") == ["date"]


def is_first_data_read(body):
    """gsc._first_data: the whole kept history by day. The coverage read sends no rowLimit."""
    return is_coverage_read(body) and "rowLimit" in body


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


async def test_a_ga4_default_range_is_the_same_at_each_hour_of_1_day(live, fake_net, monkeypatch):
    calls = analytics(fake_net, "America/Los_Angeles")
    seen = set()
    # Each 3rd hour of 2026-03-17, the local date of the machine in these tests.
    for moment in HOURS[:8]:
        freeze(monkeypatch, moment)
        coverage = (await source("ga4_organic"))["coverage"]
        seen.add((*span(coverage), coverage["last_complete_day"], coverage["partial_days"]))
    assert seen == {("2026-02-15", "2026-03-14", "2026-03-14", None)}
    sent = {
        json.dumps(json.loads(call.content)["dateRanges"])
        for call in calls
        if call.url.host == ga4.HOST
    }
    assert sent == {json.dumps([{"startDate": "2026-02-15", "endDate": "2026-03-14"}])}


async def test_last_days_gives_the_dates_that_went_out(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16")
    coverage = (await source("search_queries", last_days=7))["coverage"]
    assert span(coverage) == ("2026-03-09", FIXED_END)
    assert (reports(calls)[0]["startDate"], reports(calls)[0]["endDate"]) == span(coverage)

    analytics(fake_net)
    coverage = (await source("ga4_organic", last_days=7))["coverage"]
    assert span(coverage) == ("2026-03-09", "2026-03-15")


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
    result = await source("search_queries", start_date="2026-03-10", end_date=NEWEST)
    assert result["rows"], "A partial day keeps its data."
    assert result["coverage"]["partial_days"] == {"from": "2026-03-16", "to": NEWEST}
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
        if is_first_data_read(body):
            # The property collects from 2026-01-01, so a later day with no rows is a real 0.
            answer["rows"] = [row("2026-01-01", ["date"])]
        elif is_coverage_read(body):
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
        sent = json.loads(request.content)
        if sent["metrics"] == [{"name": "eventCount"}]:
            # The property collects from 2026-01-01, so a later day with no rows is a real 0.
            first = {"dimensionValues": [{"value": "20260101"}], "metricValues": [{"value": "9"}]}
            return httpx2.Response(200, json={**body, "rows": [first]})
        if sent["dateRanges"][0]["startDate"] == day:
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
    # engagementRate is engagedSessions / sessions: with 0 sessions it is 0/0, so no rate.
    assert result["totals"] == {
        "sessions": 0,
        "totalUsers": 0,
        "engagementRate": None,
        "keyEvents": 0,
    }
    assert result["delta"] == {
        "sessions": -40,
        "totalUsers": -30,
        "engagementRate": None,
        "keyEvents": -2,
    }
    assert "unknown" not in (result["reason"] or "")


async def test_ga4_totals_from_the_api_stay_as_ga4_sends_them(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    ga4_day_has_sessions(fake_net, "2026-03-10", 0)
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-03-10")
    assert result["totals"] == {
        "sessions": 0,
        "totalUsers": 30,
        "engagementRate": 0.5,
        "keyEvents": 2,
    }
    assert result["rows"][0]["engagementRate"] == 0.5


# A rate or an average divides by a count. With no rows the count is 0, so the value is 0/0.
RATIO_WORDS = ("Rate", "average", "Per")


def test_each_ga4_metric_has_an_empty_total_that_is_no_false_0():
    wrong = sorted(
        name
        for name, empty in ga4.EMPTY_TOTALS.items()
        if empty is not None and any(word in name for word in RATIO_WORDS)
    )
    fix = (
        "In ga4.EMPTY_TOTALS, give a rate or an average None: with no sessions it is 0/0, "
        "and a 0 shows a false drop in delta."
    )
    assert not wrong, f"{fix} {wrong}"
    missing = sorted(set(ga4.METRICS) ^ set(ga4.EMPTY_TOTALS))
    assert not missing, f"Give each GA4 metric its empty total in ga4.EMPTY_TOTALS: {missing}"


async def test_a_metric_that_is_null_on_1_side_has_a_null_delta(live, fake_net):
    page = f"https://{HOST}/a"
    traffic = {"metricName": "Traffic", "information": [{"totalSessionCount": "110", "Url": page}]}
    fake_net(lambda _: httpx2.Response(200, json=[traffic]))
    result = await source("clarity", page="/a", compare_page="/c")
    assert result["totals"] == {"sessions": 110}
    assert result["comparison"]["totals"] == {"sessions": None}
    assert result["delta"] == {"sessions": None}


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


async def test_a_saved_search_console_estimate_serves_only_on_its_own_day(
    live, fake_net, monkeypatch
):
    """George's rule: no time. An estimate comes from today, so its saved report keys on today:
    after midnight the report goes out again, and the estimate is of the new day."""
    calls = search_console(fake_net)
    named = {"start_date": "2026-03-10", "end_date": "2026-03-16"}
    freeze(monkeypatch, datetime(2026, 3, 18, 23, 58, tzinfo=UTC))
    first = await source("search_queries", **named)
    freeze(monkeypatch, datetime(2026, 3, 19, 0, 1, tzinfo=UTC))
    second = await source("search_queries", **named)
    assert len(reports(calls)) == 4, "The report of a new day must not come from the saved one."
    assert first["coverage"]["last_complete_day"] == ESTIMATE
    assert second["coverage"]["last_complete_day"] == "2026-03-15"
    assert second["from_cache"] is False


async def test_a_saved_report_of_a_partial_day_never_shows_it_complete(live, fake_net, monkeypatch):
    t0 = datetime(2026, 3, 18, 20, 0, tzinfo=UTC)
    # The range ends after both API days, so both reports send dataState all: only the key differs.
    named = {"start_date": "2026-03-10", "end_date": NEWEST}
    freeze(monkeypatch, t0)
    search_console(fake_net, "2026-03-16", days=["2026-03-16"])
    await source("search_pages")
    freeze(monkeypatch, t0 + timedelta(seconds=200))
    first = await source("search_queries", **named)
    assert first["coverage"]["partial_days"]["from"] == "2026-03-16"
    # Google moves its day: the coverage entry ends at t0+300 s, and the report entry is still good.
    freeze(monkeypatch, t0 + timedelta(seconds=350))
    calls = search_console(fake_net, "2026-03-17", days=["2026-03-16"])
    second = await source("search_queries", **named)
    assert second["coverage"]["last_complete_day"] == "2026-03-16"
    assert len(reports(calls)) == 2, "Rows of a partial day must not serve as complete."
    assert second["from_cache"] is False


async def test_a_saved_query_api_answer_never_shows_a_partial_day_complete(
    live, fake_net, monkeypatch
):
    t0 = datetime(2026, 3, 18, 20, 0, tzinfo=UTC)
    request = {"startDate": "2026-03-10", "endDate": "2026-03-18", "dimensions": ["page"]}
    freeze(monkeypatch, t0)
    search_console(fake_net, "2026-03-17", days=["2026-03-17"])
    await source("search_pages")
    freeze(monkeypatch, t0 + timedelta(seconds=200))
    await search_analytics(request)
    # Google moves its day: the coverage entry ends at t0+300 s, and the query entry is still good.
    freeze(monkeypatch, t0 + timedelta(seconds=350))
    calls = search_console(fake_net, "2026-03-18", days=["2026-03-17"])
    second = await search_analytics(request)
    assert second["coverage"]["last_complete_day"] == "2026-03-17"
    assert len(reports(calls)) == 1, "Rows of a partial day must not serve as complete."
    assert second["from_cache"] is False


@pytest.mark.parametrize("text", [server.GET_SEO_DATA, server.QUERY_API], ids=["get", "query"])
async def test_a_live_answer_shows_the_time_of_its_oldest_part(live, fake_net, monkeypatch, text):
    """A live test: an answer with from_cache false showed an older cached_at with no reason."""
    t0 = datetime(2026, 3, 18, 20, 0, tzinfo=UTC)
    freeze(monkeypatch, t0)
    search_console(fake_net, "2026-03-17")
    await source("search_queries")
    freeze(monkeypatch, t0 + timedelta(seconds=100))
    result = await source("search_pages")
    assert result["from_cache"] is False
    assert result["cached_at"] == t0.isoformat()
    flat = " ".join(text.lower().split())
    assert "the oldest part gives cached_at and cache_age_seconds" in flat
    assert "from_cache false can show" in flat


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


async def test_query_api_marks_an_estimate_when_no_read_gives_the_day(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net)
    data = await search_analytics({"startDate": "2026-03-10", "endDate": NEWEST})
    # The coverage read of get_seo_data, then the query.
    assert len(bodies(calls)) == 2
    coverage = data["coverage"]
    assert coverage["last_complete_day"] == ESTIMATE
    assert coverage["last_complete_day_source"] == "estimate"
    assert "dataState all" in coverage["last_complete_day_how"]
    assert coverage["data_state"] == "final"
    assert coverage["partial_days"] == {"from": "2026-03-15", "to": NEWEST}


DAY_FIELDS = ("last_complete_day", "last_complete_day_source", "partial_days")


async def test_both_tools_give_the_same_coverage_of_the_same_days(live, fake_net, monkeypatch):
    """A live test: get_seo_data gave the API day, and query_api an estimate of the same days."""
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    seo = await source("search_pages", start_date="2026-02-16", end_date=FIXED_END)
    request = {"startDate": "2026-02-16", "endDate": FIXED_END, "dimensions": ["page"]}
    api = await search_analytics(request)
    assert {key: api["coverage"][key] for key in DAY_FIELDS} == {
        key: seo["coverage"][key] for key in DAY_FIELDS
    }
    assert api["coverage"]["last_complete_day_source"] == "api"
    assert "incomplete" not in api
    assert "incomplete" not in seo


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


async def test_ga4_names_its_last_complete_day_as_a_plain_date(live, fake_net, monkeypatch):
    freeze(monkeypatch, NIGHT)
    analytics(fake_net, "Asia/Tokyo")
    coverage = (await source("ga4_landing_pages"))["coverage"]
    assert coverage["time_zone"] == "Asia/Tokyo"
    assert coverage["last_complete_day"] == "2026-03-15"
    assert coverage["last_complete_day_source"] == "estimate"
    assert coverage["partial_days"] is None


async def test_a_ga4_range_with_a_partial_day_is_marked(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    result = await source("ga4_organic", start_date="2026-03-10", end_date=NEWEST)
    # The last complete day is 3 days before today (2026-03-18), so the 16th and 17th are partial.
    assert result["coverage"]["partial_days"] == {"from": "2026-03-16", "to": NEWEST}


@pytest.mark.parametrize(
    ("start", "end", "sent", "partial"),
    [
        ("7daysAgo", "today", ("2026-03-11", NEWEST), {"from": "2026-03-16", "to": NEWEST}),
        ("1daysAgo", "today", (NEWEST, NEWEST), {"from": NEWEST, "to": NEWEST}),
        ("30daysAgo", "3daysAgo", ("2026-02-16", "2026-03-15"), None),
        ("2026-03-01", "today", ("2026-03-01", NEWEST), {"from": "2026-03-16", "to": NEWEST}),
        ("2026-03-16", "yesterday", ("2026-03-16", NEWEST), {"from": "2026-03-16", "to": NEWEST}),
        ("10daysAgo", "2026-03-18", ("2026-03-08", NEWEST), {"from": "2026-03-16", "to": NEWEST}),
        ("4daysAgo", NEWEST, ("2026-03-14", NEWEST), {"from": "2026-03-16", "to": NEWEST}),
        ("2026-01-01", "2026-01-31", ("2026-01-01", "2026-01-31"), None),
    ],
)
async def test_ga4_query_api_marks_each_kind_of_range(
    live, fake_net, monkeypatch, start, end, sent, partial
):
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    request = {"dateRanges": [{"startDate": start, "endDate": end}], "metrics": [{"name": "x"}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    (coverage,) = data["coverage"]
    assert span(coverage) == sent
    assert coverage["partial_days"] == partial


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "Pacific/Pago_Pago", "Not/AZone", None])
async def test_the_name_of_a_ga4_zone_changes_no_date(live, fake_net, monkeypatch, zone):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, zone)
    result = await source("ga4_organic", start_date="2026-03-10", end_date=NEWEST)
    assert result["status"] == "ok"
    assert span(result["coverage"]) == ("2026-03-10", NEWEST)
    assert result["coverage"]["partial_days"] == {"from": "2026-03-16", "to": NEWEST}
    assert [
        json.loads(call.content)["dateRanges"] for call in calls if call.url.host == ga4.HOST
    ] == [[{"startDate": "2026-03-10", "endDate": NEWEST}]]


async def test_a_saved_ga4_answer_serves_only_on_its_own_day(live, fake_net, monkeypatch):
    """George's rule: no time. The coverage of a saved answer is of the day of its call, so the
    day is in the key: after midnight the report goes out again."""
    calls = analytics(fake_net, "UTC")
    request = {
        "dateRanges": [
            {"startDate": "2026-03-10", "endDate": "2026-03-16"},
            {"startDate": "30daysAgo", "endDate": "3daysAgo"},
        ],
        "metrics": [{"name": "sessions"}],
    }
    freeze(monkeypatch, datetime(2026, 3, 18, 23, 58, tzinfo=UTC))
    first = await core.query_api(HOST, "ga4", "run_report", request)
    freeze(monkeypatch, datetime(2026, 3, 19, 0, 1, tzinfo=UTC))
    second = await core.query_api(HOST, "ga4", "run_report", request)
    assert len(sent_bodies(calls, ga4.HOST)) == 2
    assert second["from_cache"] is False
    assert first["coverage"][0]["partial_days"] == {"from": "2026-03-16", "to": "2026-03-16"}
    assert second["coverage"][0]["partial_days"] is None
    assert second["applied"]["dateRanges"][1] == {
        "startDate": "2026-02-17",
        "endDate": "2026-03-16",
    }


# An end past the newest day of a source moves to that day, and the result shows the move.


def sent_bodies(calls, host):
    return [json.loads(call.content) for call in calls if call.url.host == host]


async def test_a_search_console_end_past_yesterday_moves_to_yesterday(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net, "2026-03-16", days=["2026-03-17"])
    result = await source("search_queries", start_date="2026-03-10", end_date="2026-04-01")
    assert result["status"] == "ok"
    assert result["date_moved"] == {
        "asked_end_date": "2026-04-01",
        "sent_end_date": NEWEST,
    }
    assert f"moved from 2026-04-01 to {NEWEST}" in result["reason"]
    reports = [body for body in bodies(calls) if not is_coverage_read(body)]
    assert {body["endDate"] for body in reports} == {NEWEST}
    assert result["coverage"]["end_date"] == NEWEST


def test_an_end_up_to_yesterday_never_moves_in_each_source(monkeypatch):
    """Each day of 2 years: an end up to today minus 1 goes out as it is, and a later end goes
    out as today minus 1. No hour and no zone take part."""
    for offset in range(0, 730, 7):
        today = date(2026, 1, 1) + timedelta(days=offset)
        monkeypatch.setattr(params, "_clock", lambda today=today: today)
        newest = today - timedelta(days=1)
        for start in (today - timedelta(days=5), newest):
            for end in (newest - timedelta(days=1), newest, today, today + timedelta(days=2)):
                if start > end:
                    continue
                case = (today, start, end)
                want = min(end, newest)
                assert gsc._newest_end("start_date", start, end) == want, case
                sent = ga4._sent_dates("start_date", start.isoformat(), end.isoformat())
                assert sent == (start.isoformat(), want.isoformat(), end > newest), case


async def test_an_end_inside_the_source_days_does_not_move(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source("search_queries", start_date="2026-03-10", end_date=NEWEST)
    assert "date_moved" not in result


async def test_a_ga4_end_past_yesterday_goes_out_as_yesterday(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, "Asia/Tokyo")
    result = await source("ga4_organic", start_date="2026-03-10", end_date="2026-04-01")
    assert sent_bodies(calls, ga4.HOST)[0]["dateRanges"][0]["endDate"] == NEWEST
    assert result["date_moved"] == {"asked_end_date": "2026-04-01", "sent_end_date": NEWEST}
    assert result["coverage"]["end_date"] == NEWEST
    assert "2026-04-01" in result["reason"]
    assert NEWEST in result["reason"]


@pytest.mark.parametrize(
    ("moment", "local", "newest"),
    [
        # The machine is ahead of UTC: its date is the 19th while the UTC date is the 18th.
        (datetime(2026, 3, 18, 20, 0, tzinfo=UTC), date(2026, 3, 19), "2026-03-18"),
        # The machine is behind UTC: its date is the 18th while the UTC date is the 19th.
        (datetime(2026, 3, 19, 2, 0, tzinfo=UTC), date(2026, 3, 18), "2026-03-17"),
    ],
    ids=["ahead", "behind"],
)
async def test_the_local_date_and_not_the_utc_date_gives_the_newest_day(
    live, fake_net, monkeypatch, moment, local, newest
):
    freeze(monkeypatch, moment)
    monkeypatch.setattr(params, "_clock", lambda: local)
    search_console(fake_net, "2026-03-10")
    sources = (
        await core.get_seo_data(
            HOST, ["ga4_organic", "search_queries"], start_date="2026-03-10", end_date="2026-03-25"
        )
    )["sources"]
    for name in ("ga4_organic", "search_queries"):
        assert sources[name]["date_moved"]["sent_end_date"] == newest, name


async def test_a_ga4_move_can_never_reverse_a_range(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net, "America/Los_Angeles")
    await source("ga4_organic", start_date=NEWEST, end_date="2026-04-01")
    sent = sent_bodies(calls, ga4.HOST)[0]["dateRanges"]
    assert sent == [{"startDate": NEWEST, "endDate": NEWEST}]


def test_a_start_after_yesterday_is_refused_on_each_day(monkeypatch):
    for offset in range(0, 730, 7):
        today = date(2026, 1, 1) + timedelta(days=offset)
        monkeypatch.setattr(params, "_clock", lambda today=today: today)
        for start in (today, today + timedelta(days=1)):
            end = start + timedelta(days=3)
            with pytest.raises(params.BadInput, match="most recent day"):
                gsc._newest_end("start_date", start, end)
            with pytest.raises(params.BadInput, match="most recent day"):
                ga4._sent_dates("start_date", start.isoformat(), end.isoformat())


async def test_query_api_moves_a_search_console_end(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    calls = search_console(fake_net)
    data = await search_analytics({"startDate": "2026-03-10", "endDate": "2026-04-01"})
    assert bodies(calls)[-1]["endDate"] == NEWEST
    assert data["applied"]["endDate"] == NEWEST
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
    assert [item["endDate"] for item in sent] == [NEWEST, "2026-02-10", NEWEST]
    assert data["date_moved"] == [
        {"asked_end_date": "2026-04-01", "sent_end_date": NEWEST},
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
    assert result["range_days"] == {"main": 7, "comparison": 20}
    assert "7 and 20 days" in result["reason"]
    assert "unequal" in result["reason"]
    assert "date_moved" not in result["comparison"]
    assert result["comparison"]["coverage"]["end_date"] == "2026-02-20"


async def test_a_ga4_move_gives_an_exact_length(live, fake_net, monkeypatch):
    """GA4 gets plain dates, so the length of a moved range is exact."""
    freeze(monkeypatch, MORNING)
    analytics(fake_net, "America/Los_Angeles")
    result = await source(
        "ga4_organic",
        start_date="2026-03-11",
        end_date="2026-04-01",
        compare_start_date="2026-02-01",
        compare_end_date="2026-02-08",
    )
    assert result["range_days"] == {"main": 7, "comparison": 8}
    assert "7 and 8 days" in result["reason"]


async def test_a_move_that_keeps_2_ranges_equal_adds_no_note(live, fake_net, monkeypatch):
    freeze(monkeypatch, MORNING)
    search_console(fake_net, "2026-03-16")
    result = await source(
        "search_queries",
        start_date="2026-03-11",
        end_date="2026-03-30",
        compare_start_date="2026-02-01",
        compare_end_date="2026-02-07",
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
# A relative date becomes a plain date of today (2026-03-18 at MORNING) before any check.
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("yesterday", "3daysAgo"),
        ("today", "yesterday"),
        ("2daysAgo", "5daysAgo"),
        ("2026-03-19", "yesterday"),
        ("today", "2026-03-16"),
    ],
)
async def test_a_ga4_relative_start_after_the_end_sends_nothing(
    live, fake_net, monkeypatch, start, end
):
    freeze(monkeypatch, MORNING)
    calls = fake_net(replies(lambda _: None))
    request = {"dateRanges": [{"startDate": start, "endDate": end}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "error"
    assert "dateRanges startDate must not be after the end date" in data["reason"]
    assert "sent nothing" in data["reason"]
    sent_nothing(calls)


# Each of these is a valid range in some property zone, so it goes out as asked.
@pytest.mark.parametrize(
    ("start", "end", "sent"),
    [
        ("3daysAgo", "3daysAgo", ("2026-03-15", "2026-03-15")),
        ("7daysAgo", "yesterday", ("2026-03-11", NEWEST)),
        (NEWEST, "yesterday", (NEWEST, NEWEST)),
        ("2daysAgo", NEWEST, ("2026-03-16", NEWEST)),
    ],
)
async def test_a_valid_ga4_range_goes_out_as_plain_dates(
    live, fake_net, monkeypatch, start, end, sent
):
    freeze(monkeypatch, MORNING)
    calls = analytics(fake_net)
    request = {"dateRanges": [{"startDate": start, "endDate": end}]}
    data = await core.query_api(HOST, "ga4", "run_report", request)
    assert data["status"] == "ok", data["reason"]
    went = [json.loads(call.content) for call in calls if call.url.host == ga4.HOST]
    assert [body["dateRanges"] for body in went] == [[{"startDate": sent[0], "endDate": sent[1]}]]
    assert "date_moved" not in data


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


ENDS = sorted(name for name, accepts in providers.ACCEPTS.items() if "end_date" in accepts)


@pytest.mark.parametrize("name", ENDS)
async def test_applied_shows_the_end_date_that_went_out(live, monkeypatch, name):
    """A live test: applied showed the asked end 2026-11-12, and GA4 sent today.

    The tool text says that a source that ran shows the values that it used in applied.
    """
    freeze(monkeypatch, MORNING)
    data = await core.get_seo_data(
        HOST,
        [name],
        start_date="2026-03-10",
        end_date="2026-04-01",
        compare_start_date="2026-03-01",
        compare_end_date="2026-04-02",
    )
    result = data["sources"][name]
    assert result["status"] == "ok", result["reason"]
    for side in (result, result["comparison"]):
        sent = side["date_moved"]["sent_end_date"]
        fix = f"{name}: applied must show the sent end date of date_moved (core._asked)."
        assert side["applied"]["end_date"] == sent, fix
        assert side["applied"]["start_date"] in {"2026-03-10", "2026-03-01"}


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
    fix = (
        "A complete range with no rows has totals 0 for each count and null for each rate, "
        "and delta shows the drop of the counts:"
    )
    counts = [value for value in totals.values() if value is not None]
    drops = [value for value in delta.values() if value is not None and value < 0]
    shown = bool(counts) and not any(counts) and bool(drops)
    assert shown, f"{name}: {fix} totals {result['totals']}, delta {result.get('delta')}"


@pytest.mark.parametrize("name", sorted(COVERED))
async def test_each_covered_source_shows_coverage(live, name):
    assert {"start_date", "end_date", "last_complete_day"} <= set((await source(name))["coverage"])


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


def _calls(function: ast.AST) -> set[str]:
    return {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}


def test_each_search_console_coverage_starts_from_the_shared_read():
    """A live test: query_api read only its own reply, and gave the same days an estimate.

    Each function that builds a Search Console coverage also reads gsc._last_complete, the read
    that get_seo_data uses, so that the 2 tools give the same days the same answer.
    """
    path = SOURCE / "providers" / "gsc.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wrong = [
        f"{path.name}:{node.lineno} {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and "_coverage" in _calls(node)
        and "_last_complete" not in _calls(node)
    ]
    fix = "Read the day with _last_complete before _coverage, and use _shown for an estimate:"
    assert not wrong, f"{fix} {wrong}"
