"""A delta never compares a range in which the source collected no data.

George: "the comparison needs to be same dates". GA4 leaves out each day with 0 sessions, and a
small site has many real 0 days, so a missing row does not prove that GA4 collected nothing.
The best fact that the API gives: the first day on which the property reports any event.
"""

import json
from datetime import UTC, datetime

import httpx2
import pytest

from seo_mcp import core, providers
from seo_mcp.providers import ga4
from test_coverage import QUERY, freeze, is_first_data_read
from test_query_api import HOST, ga4_kind
from test_quota import replies

# The live case: GA4 started to collect on 2026-10-08. The clock stays in the past, because
# shared._prune compares the real time of each saved file with it.
ANSWERED = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
MAIN = {"start_date": "2026-10-10", "end_date": "2026-11-06"}
COMPARE = {"compare_start_date": "2026-09-12", "compare_end_date": "2026-10-09"}
FIRST_EVENT = "20261008"


def is_probe(body):
    return [metric["name"] for metric in body["metrics"]] == ["eventCount"]


def day_row(day, sessions):
    values = [str(sessions), "1", "0.5", "0"]
    return {"dimensionValues": [{"value": day}], "metricValues": [{"value": v} for v in values]}


def search_console_from(first_day, status=200):
    """Answer the first data read of Search Console with 1 row a day from `first_day`."""

    def reply(request):
        if not request.url.path.endswith(QUERY):
            return None
        if not is_first_data_read(json.loads(request.content)):
            return None
        if status != 200:
            return httpx2.Response(status)
        # Search Console sorts rows by clicks, not by day, so the first day is not row 0.
        days = ["2026-11-01", first_day] if first_day else []
        cells = {"clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}
        rows = [{"keys": [day], **cells} for day in days]
        return httpx2.Response(200, json={"responseAggregationType": "byProperty", "rows": rows})

    return reply


def property_with(days, first_event=FIRST_EVENT, probe_status=200, gsc_first="2025-06-01"):
    """Answer each organic report with the rows of `days` in its range, as GA4 does."""
    console = search_console_from(gsc_first)

    def reply(request):
        if request.url.host != ga4.HOST:
            return console(request)
        body = json.loads(request.content)
        answer: dict = {"metadata": {"timeZone": "UTC"}, **ga4_kind(request)}
        if is_probe(body):
            if probe_status != 200:
                return httpx2.Response(probe_status)
            if first_event:
                answer["rows"] = [
                    {"dimensionValues": [{"value": first_event}], "metricValues": [{"value": "7"}]}
                ]
            return httpx2.Response(200, json=answer)
        window = body["dateRanges"][0]
        start, end = (window[key].replace("-", "") for key in ("startDate", "endDate"))
        end = "99999999" if end == "today" else end
        rows = [day_row(day, sessions) for day, sessions in days.items() if start <= day <= end]
        if rows:
            total = sum(int(row["metricValues"][0]["value"]) for row in rows)
            answer["metricHeaders"] = [{"name": name} for name in ga4.METRICS]
            answer["rows"] = rows
            answer["totals"] = [{"metricValues": day_row("", total)["metricValues"]}]
        return httpx2.Response(200, json=answer)

    return reply


def probes(calls):
    return [
        call for call in calls if call.url.host == ga4.HOST and is_probe(json.loads(call.content))
    ]


def first_data_reads(calls):
    return [
        call
        for call in calls
        if call.url.path.endswith(QUERY) and is_first_data_read(json.loads(call.content))
    ]


# The live case: each main day has 1 session except 2026-10-16, which has none.
LIVE_DAYS = {
    f"202610{day:02d}" if day <= 31 else f"202611{day - 31:02d}": 1
    for day in range(10, 38)
    if day != 16
}
LIVE_DAYS |= {"20261008": 2, "20261009": 1}


async def test_the_live_case_gives_no_delta_and_names_the_first_day(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with(LIVE_DAYS)))
    data = await core.get_seo_data(HOST, ["search_pages", "ga4_organic"], **MAIN, **COMPARE)
    result = data["sources"]["ga4_organic"]
    assert result["status"] == "ok"
    assert result["comparison"]["totals"]["sessions"] == 3
    assert result["delta"] is None
    assert "2026-10-08" in result["reason"]
    assert "on or after 2026-10-08" in result["reason"]
    assert result["comparison"]["first_data_day"] == "2026-10-08"
    # The asked ranges stay as asked: the server moves no period.
    assert result["comparison"]["applied"]["start_date"] == "2026-09-12"
    assert "date_moved" not in result["comparison"]
    assert len(probes(calls)) == 1
    # Search Console collected before both ranges, so its delta stays, after 1 extra read.
    assert data["sources"]["search_pages"]["delta"] is not None
    assert data["sources"]["search_pages"]["comparison"]["first_data_day"] == "2025-06-01"
    assert len(first_data_reads(calls)) == 1


async def test_real_0_days_after_the_first_event_keep_the_delta(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    days = {"20260920": 4, "20261010": 9}
    calls = fake_net(replies(property_with(days, first_event="20250101")))
    data = await core.get_seo_data(HOST, ["ga4_organic"], **MAIN, **COMPARE)
    result = data["sources"]["ga4_organic"]
    assert result["delta"]["sessions"] == 5
    assert result["comparison"]["first_data_day"] == "2025-01-01"
    assert len(probes(calls)) == 1


async def test_a_normal_comparison_sends_no_extra_read(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    days = {"20260912": 4, "20261010": 9}
    calls = fake_net(replies(property_with(days)))
    data = await core.get_seo_data(HOST, ["ga4_organic"], **MAIN, **COMPARE)
    result = data["sources"]["ga4_organic"]
    assert result["delta"]["sessions"] == 5
    assert "first_data_day" not in result["comparison"]
    assert probes(calls) == []


async def test_a_single_range_sends_no_extra_read(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with({"20261020": 3})))
    await core.get_seo_data(HOST, ["ga4_organic"], **MAIN)
    assert probes(calls) == []


async def test_a_failed_extra_read_keeps_the_totals_and_gives_no_delta(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with(LIVE_DAYS, probe_status=500)))
    data = await core.get_seo_data(HOST, ["ga4_organic"], **MAIN, **COMPARE)
    result = data["sources"]["ga4_organic"]
    assert result["status"] == result["comparison"]["status"] == "ok"
    assert result["comparison"]["totals"]["sessions"] == 3
    assert result["delta"] is None
    assert "could not check" in result["reason"]
    assert len(probes(calls)) == 1


async def test_a_property_with_no_event_gives_no_delta(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    fake_net(replies(property_with({"20261010": 1}, first_event=None)))
    data = await core.get_seo_data(HOST, ["ga4_organic"], **MAIN, **COMPARE)
    result = data["sources"]["ga4_organic"]
    assert result["delta"] is None
    assert "no event" in result["reason"]


async def test_landing_pages_share_1_extra_read_with_the_daily_source(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with(LIVE_DAYS)))
    data = await core.get_seo_data(HOST, ["ga4_organic", "ga4_landing_pages"], **MAIN, **COMPARE)
    pages = data["sources"]["ga4_landing_pages"]
    # A landing page report has no day rows, so it cannot see a gap: it always checks.
    assert pages["delta"] is None
    assert "2026-10-08" in pages["reason"]
    assert len(probes(calls)) == 1


# Search Console: "Data collection for a property starts as soon anyone adds the property".


async def test_a_search_console_property_added_after_the_start_gives_no_delta(
    live, fake_net, monkeypatch
):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with({}, gsc_first="2026-10-01")))
    data = await core.get_seo_data(HOST, ["search_queries", "search_pages"], **MAIN, **COMPARE)
    for name in ("search_queries", "search_pages"):
        result = data["sources"][name]
        assert result["status"] == result["comparison"]["status"] == "ok"
        assert result["delta"] is None
        assert "on or after 2026-10-01" in result["reason"]
    # The body changes only with the UTC date, so both sources and both ranges share 1 read.
    assert len(first_data_reads(calls)) == 1


async def test_a_search_console_range_alone_sends_no_extra_read(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    calls = fake_net(replies(property_with({})))
    await core.get_seo_data(HOST, ["search_queries"], **MAIN)
    assert first_data_reads(calls) == []


async def test_a_failed_search_console_read_gives_no_delta(live, fake_net, monkeypatch):
    freeze(monkeypatch, ANSWERED)
    console = search_console_from("2025-06-01", status=500)
    fake_net(replies(lambda request: console(request)))
    data = await core.get_seo_data(HOST, ["search_queries"], **MAIN, **COMPARE)
    result = data["sources"]["search_queries"]
    assert result["status"] == "ok"
    assert result["totals"] is not None
    assert result["delta"] is None
    assert "could not check" in result["reason"]


# A new dated source must check its first day of data too, or its delta can compare no data.
DATED = sorted(name for name, names in providers.ACCEPTS.items() if "start_date" in names)


@pytest.mark.parametrize("name", DATED)
async def test_each_dated_source_checks_its_first_day_of_data(live, fake_net, monkeypatch, name):
    freeze(monkeypatch, ANSWERED)
    fake_net(replies(property_with({})))
    result = (await core.get_seo_data(HOST, [name], **MAIN, **COMPARE))["sources"][name]
    fix = (
        "In a date comparison, a range with no row on its first day must read the first day "
        "of data of the source and give first_data_day and first_data_read (see "
        "ga4._first_event, gsc._first_data), so that core gives no delta for days with no data:"
    )
    sides = {"main": result, "comparison": result.get("comparison", {})}
    missing = sorted(side for side, done in sides.items() if "first_data_read" not in done)
    assert not missing, f"{name}: {fix} {missing}"
