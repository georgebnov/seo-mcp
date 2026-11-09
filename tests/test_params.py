"""The simple path: a time frame, a page, a query, and a comparison on get_seo_data."""

import json
from datetime import timedelta

import httpx2
import pytest

from seo_mcp import core, params, providers, shared
from seo_mcp.providers import clarity
from test_query_api import clarity_auth

SITE = {
    "gsc_property": "sc-domain:example.com",
    "ga4_property_id": "123456789",
    "bing_site_url": "https://www.example.com/",
    "secrets": {"clarity_token": {"env": "CLARITY_ENV"}},
}
PAGE = "https://www.example.com/a"
OTHER = "https://www.example.com/b"
ORGANIC = {
    "filter": {
        "fieldName": "sessionDefaultChannelGroup",
        "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
    }
}
TRAFFIC = {
    "metricName": "Traffic",
    "information": [
        {"totalSessionCount": "20", "Url": PAGE},
        {"totalSessionCount": "90", "Url": "http://www.example.com/a"},
        {"totalSessionCount": "500", "Url": OTHER},
    ],
}
GOOD_DAY = "2026-01-10"


def days_ago(days: int) -> str:
    return (params.today() - timedelta(days=days)).isoformat()


def search_console_days_ago(days: int) -> str:
    """This fake has no firstIncompleteDate, so a default range ends on the estimate: 3 days
    before the newest day, today minus 1."""
    return (params.today() - timedelta(days=days + 1)).isoformat()


def body_of(request) -> dict:
    return json.loads(request.content)


def is_first_data_read(body: dict) -> bool:
    return body.get("metrics") == [{"name": "eventCount"}]


def reports(calls) -> list[dict]:
    """The report bodies, without the read that finds the last complete day."""
    bodies = [body_of(call) for call in api_calls(calls)]
    return [
        body
        for body in bodies
        if not (body.get("dimensions") == ["date"] and "rowLimit" not in body)
    ]


def gsc_row(keys, clicks=9, impressions=500):
    return {"keys": keys, "clicks": clicks, "impressions": impressions, "ctr": 0.1, "position": 5.0}


def answer(request: httpx2.Request) -> httpx2.Response:
    """1 fake for every API. It answers from what the request asked for."""
    host, path = request.url.host, request.url.path
    if host == "oauth2.googleapis.com":
        return httpx2.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
    if path.endswith("/searchAnalytics/query"):
        dimensions = body_of(request).get("dimensions", [])
        return httpx2.Response(200, json={"rows": [gsc_row([f"/{d}" for d in dimensions])]})
    if path.endswith("/sitemaps"):
        return httpx2.Response(200, json={"sitemap": []})
    if path.endswith("index:inspect"):
        return httpx2.Response(200, json={"inspectionResult": {"indexStatusResult": {}}})
    if path.endswith(":runReport"):
        name = body_of(request)["dimensions"][0]["name"]
        return httpx2.Response(
            200,
            json={
                "metricHeaders": [{"name": "sessions"}],
                "rows": [
                    {
                        "dimensionValues": [{"value": f"{name}-1"}],
                        "metricValues": [{"value": "150"}],
                    }
                ],
                "totals": [{"metricValues": [{"value": "150"}]}],
                "kind": "analyticsData#runReport",
            },
        )
    if path.endswith("runPagespeed"):
        return httpx2.Response(200, json={"lighthouseResult": {"categories": {}, "audits": {}}})
    if path.endswith("queryRecord"):
        return httpx2.Response(200, json={"record": {"metrics": {}}})
    if "bing" in host:
        return httpx2.Response(200, json={"d": {"Links": [], "TotalPages": 0}})
    if "clarity" in host:
        return httpx2.Response(200, json=[TRAFFIC])
    return httpx2.Response(500)


@pytest.fixture
def live(write_config, fake_net, account, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-1234")
    monkeypatch.setenv("BING_KEY_ENV", "bing-key-value-5678")
    shared = {
        **account,
        "google_api_key": {"env": "GOOGLE_KEY_ENV"},
        "bing_api_key": {"env": "BING_KEY_ENV"},
    }
    write_config(SITE, shared)
    return fake_net(answer)


def api_calls(calls) -> list[httpx2.Request]:
    return [call for call in calls if call.url.host != "oauth2.googleapis.com"]


async def ask(sources, **params):
    return await core.get_seo_data("www.example.com", sources, **params)


# Input that is wrong for every source stops the call before any network request.
@pytest.mark.parametrize(
    ("params", "named"),
    [
        ({"start_date": "2026-1-5", "end_date": GOOD_DAY}, "start_date"),
        ({"start_date": "20260105", "end_date": GOOD_DAY}, "start_date"),
        ({"start_date": "2026-02-30", "end_date": "2026-03-01"}, "start_date"),
        ({"start_date": GOOD_DAY}, "end_date"),
        ({"end_date": GOOD_DAY}, "start_date"),
        ({"start_date": "2026-01-11", "end_date": GOOD_DAY}, "start_date"),
        ({"start_date": GOOD_DAY, "end_date": GOOD_DAY, "last_days": 7}, "last_days"),
        ({"last_days": 0}, "last_days"),
        ({"last_days": 100000}, "last_days"),
        ({"page": ""}, "page"),
        ({"page": "a"}, "page"),
        ({"page": "//www.evil.test/a"}, "page"),
        ({"page": "https://www.evil.test/a"}, "page"),
        ({"page": "https://www.example.com@www.evil.test/a"}, "page"),
        ({"page": "https://user@www.example.com/a"}, "page"),
        ({"page": "https://www.example.com:8443/a"}, "page"),
        ({"page": "http://www.example.com/a"}, "page"),
        ({"page": "/a#top"}, "page"),
        ({"page": "/a b"}, "page"),
        ({"page": "/" + "a" * 4096}, "page"),
        ({"query": ""}, "query"),
        ({"query": "a" * 4097}, "query"),
        ({"compare_page": "/b"}, "compare_page"),
        ({"page": "/a", "compare_page": "/a"}, "compare_page"),
        ({"compare_start_date": GOOD_DAY, "compare_end_date": GOOD_DAY}, "compare_start_date"),
        (
            {
                "start_date": GOOD_DAY,
                "end_date": GOOD_DAY,
                "compare_start_date": "2026-01-01",
            },
            "compare_end_date",
        ),
        (
            {
                "page": "/a",
                "compare_page": "/b",
                "start_date": GOOD_DAY,
                "end_date": GOOD_DAY,
                "compare_start_date": "2026-01-01",
                "compare_end_date": "2026-01-01",
            },
            "compare_page",
        ),
    ],
)
async def test_bad_input_stops_the_call_and_names_the_parameter(live, params, named):
    data = await ask(["search_queries"], **params)
    assert data["status"] == "error"
    assert named in data["reason"]
    assert not live


async def test_input_text_with_a_secret_shape_never_returns(live):
    data = await ask(["search_queries"], page="https://www.evil.test/?key=abc123secret")
    assert "abc123secret" not in json.dumps(data)


# No parameter means the call of today, field for field.
async def test_search_console_default_body_is_unchanged(live):
    await ask(["search_queries"])
    bodies = [body_of(call) for call in api_calls(live)]
    end = search_console_days_ago(3)
    start = search_console_days_ago(30)
    window = {"startDate": start, "endDate": end, "rowLimit": 25000}
    assert {"dimensions": ["query"], **window} in bodies
    assert {"dimensions": [], **window} in bodies


async def test_ga4_default_body_is_unchanged(live):
    await ask(["ga4_landing_pages"])
    assert body_of(api_calls(live)[0]) == {
        "dateRanges": [{"startDate": days_ago(30), "endDate": days_ago(3)}],
        "dimensions": [{"name": "landingPage"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
            {"name": "engagementRate"},
            {"name": "keyEvents"},
        ],
        "dimensionFilter": ORGANIC,
        "metricAggregations": ["TOTAL"],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 25,
        "returnPropertyQuota": True,
    }


async def test_default_result_has_only_the_fields_of_today(live):
    data = await ask(["search_queries", "sitemaps"])
    assert set(data) == {
        "status",
        "reason",
        "site",
        "thin_data",
        "thin_sources",
        "counts",
        "sources",
    }
    assert data["counts"] == {"ok": 2, "not_connected": 0, "error": 0}
    assert set(data["sources"]["search_queries"]) == {
        "status",
        "reason",
        "thin_data",
        "window",
        "totals",
        "coverage",
        "rows_total",
        "truncated",
        "rows",
        "from_cache",
        "cached_at",
        "cache_age_seconds",
        "cache_expires_at",
    }


async def test_search_console_filters_reach_rows_and_totals(live):
    data = await ask(
        ["search_queries"],
        start_date="2026-01-01",
        end_date=GOOD_DAY,
        page="/a",
        query="best shoes",
    )
    bodies = reports(live)
    assert len(bodies) == 2
    for body in bodies:
        assert body["startDate"] == "2026-01-01"
        assert body["endDate"] == GOOD_DAY
        assert body["dimensionFilterGroups"] == [
            {
                "groupType": "and",
                "filters": [
                    {"dimension": "page", "operator": "equals", "expression": PAGE},
                    {"dimension": "query", "operator": "equals", "expression": "best shoes"},
                ],
            }
        ]
    result = data["sources"]["search_queries"]
    assert result["window"] == {"start": "2026-01-01", "end": GOOD_DAY}
    assert result["applied"] == {
        "start_date": "2026-01-01",
        "end_date": GOOD_DAY,
        "page": PAGE,
        "query": "best shoes",
    }


async def test_last_days_ends_where_the_default_window_ends(live):
    data = await ask(["search_pages", "ga4_organic"], last_days=7)
    window = data["sources"]["search_pages"]["window"]
    assert window == {"start": search_console_days_ago(9), "end": search_console_days_ago(3)}
    ga4 = next(call for call in api_calls(live) if call.url.path.endswith(":runReport"))
    assert body_of(ga4)["dateRanges"] == [{"startDate": days_ago(9), "endDate": days_ago(3)}]
    assert body_of(ga4)["limit"] == 8


async def test_old_search_console_range_runs_and_says_it_is_incomplete(live):
    data = await ask(["search_pages"], start_date=days_ago(600), end_date=days_ago(590))
    result = data["sources"]["search_pages"]
    assert result["status"] == "ok"
    assert result["incomplete"] is True
    assert "16 months" in result["reason"]


async def test_search_console_end_inside_the_lag_says_so(live):
    data = await ask(["search_pages"], start_date=days_ago(10), end_date=days_ago(1))
    result = data["sources"]["search_pages"]
    assert result["incomplete"] is True
    assert "partial" in result["reason"]


async def test_no_rows_for_a_filter_is_explained(write_config, fake_net, account):
    write_config(SITE, account)

    def empty(request):
        if request.url.host == "oauth2.googleapis.com":
            return answer(request)
        # A live read with no rows gave only this field.
        return httpx2.Response(200, json={"responseAggregationType": "byProperty"})

    fake_net(empty)
    data = await ask(["search_queries"], query="Mixed Case")
    result = data["sources"]["search_queries"]
    assert result["status"] == "ok"
    assert result["thin_data"] is True
    assert "exact" in result["reason"]


async def test_ga4_custom_range_and_page(live):
    data = await ask(["ga4_landing_pages"], start_date="2026-01-01", end_date=GOOD_DAY, page="/a")
    body = body_of(api_calls(live)[0])
    assert body["dateRanges"] == [{"startDate": "2026-01-01", "endDate": GOOD_DAY}]
    assert body["dimensionFilter"] == {
        "andGroup": {
            "expressions": [
                ORGANIC,
                {
                    "filter": {
                        "fieldName": "landingPage",
                        "stringFilter": {"matchType": "EXACT", "value": "/a"},
                    }
                },
            ]
        }
    }
    result = data["sources"]["ga4_landing_pages"]
    assert result["window"] == {"startDate": "2026-01-01", "endDate": GOOD_DAY}
    assert "start" in result["page_match"]


async def test_ga4_keeps_a_query_string_of_the_page(live):
    await ask(["ga4_organic"], page="/a?x=1")
    field = body_of(api_calls(live)[0])["dimensionFilter"]["andGroup"]["expressions"][1]["filter"]
    assert field == {
        "fieldName": "landingPagePlusQueryString",
        "stringFilter": {"matchType": "EXACT", "value": "/a?x=1"},
    }


async def test_ga4_long_range_flags_the_daily_row_cap(write_config, fake_net, account):
    write_config(SITE, account)
    sent = []

    def many(request):
        if request.url.host == "oauth2.googleapis.com":
            return answer(request)
        limit = body_of(request)["limit"]
        sent.append(limit)
        day = {"dimensionValues": [{"value": "20260101"}], "metricValues": [{"value": "1"}]}
        return httpx2.Response(
            200,
            json={
                "metricHeaders": [{"name": "sessions"}],
                "rows": [day] * limit,
                "kind": "analyticsData#runReport",
            },
        )

    fake_net(many)
    data = await ask(["ga4_organic"], start_date="2025-01-01", end_date="2026-01-01")
    result = data["sources"]["ga4_organic"]
    assert sent == [101]
    assert result["truncated"] is True
    assert len(result["rows"]) == 100


async def test_index_status_with_a_page_inspects_only_that_page(live):
    data = await ask(["index_status"], page="/a")
    inspected = [body_of(call)["inspectionUrl"] for call in api_calls(live)]
    assert inspected == [PAGE]
    assert data["sources"]["index_status"]["thin_data"] is False


async def test_page_speed_and_field_data_use_the_page(live):
    await ask(["page_speed", "chrome_field_data"], page="/a")
    by_path = {call.url.path: call for call in api_calls(live)}
    assert by_path["/pagespeedonline/v5/runPagespeed"].url.params["url"] == PAGE
    body = body_of(by_path["/v1/records:queryRecord"])
    assert body == {"url": PAGE}


async def test_field_data_404_for_a_page_names_the_page(write_config, fake_net, monkeypatch):
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-1234")
    write_config({}, {"google_api_key": {"env": "GOOGLE_KEY_ENV"}})
    fake_net(lambda _: httpx2.Response(404))
    data = await ask(["chrome_field_data"], page="/a")
    result = data["sources"]["chrome_field_data"]
    assert result["status"] == "ok"
    assert "page" in result["reason"]


# A source that cannot apply a parameter does not run and says so.
async def test_source_reports_a_parameter_it_cannot_apply(live):
    data = await ask(["ga4_organic", "search_pages"], query="shoes", page="/a")
    refused = data["sources"]["ga4_organic"]
    assert refused["status"] == "unsupported"
    assert refused["unsupported_params"] == ["query"]
    assert "query" in refused["reason"]
    assert "page" in refused["accepts"]
    assert refused["rows"] == []
    assert "applied" not in refused
    assert data["sources"]["search_pages"]["status"] == "ok"
    assert data["status"] == "ok"
    assert data["counts"]["unsupported"] == 1
    assert not [call for call in api_calls(live) if call.url.path.endswith(":runReport")]


async def test_all_sources_refused_gives_a_top_status_of_unsupported(live):
    data = await ask(["sitemaps", "bing_links"], page="/a")
    assert data["status"] == "unsupported"
    assert not api_calls(live)


async def test_refused_and_not_connected_gives_not_connected(write_config, fake_net):
    write_config()
    fake_net(answer)
    data = await ask(["sitemaps", "search_pages"], query="shoes", last_days=7)
    assert data["sources"]["sitemaps"]["status"] == "unsupported"
    assert data["status"] == "not_connected"


async def test_a_source_that_takes_no_date_refuses_a_comparison_of_periods(live):
    data = await ask(
        ["page_speed"],
        start_date="2026-01-01",
        end_date=GOOD_DAY,
        compare_start_date="2025-01-01",
        compare_end_date="2025-01-10",
    )
    refused = data["sources"]["page_speed"]
    assert refused["status"] == "unsupported"
    assert refused["unsupported_params"] == [
        "compare_end_date",
        "compare_start_date",
        "end_date",
        "start_date",
    ]
    assert not api_calls(live)


async def test_compare_two_periods(live, fake_net):
    # The fake rows hold no day, so no range shows a row on its first day: each range then
    # needs the first day of GA4 data, and 1 shared read gives it.
    def first_event_2024(request):
        if request.url.path.endswith("runReport") and is_first_data_read(body_of(request)):
            row = {"dimensionValues": [{"value": "20240101"}], "metricValues": [{"value": "1"}]}
            return httpx2.Response(200, json={"kind": "analyticsData#runReport", "rows": [row]})
        return answer(request)

    live = fake_net(first_event_2024)
    data = await ask(
        ["ga4_organic"],
        start_date="2026-01-01",
        end_date=GOOD_DAY,
        compare_start_date="2025-01-01",
        compare_end_date="2025-01-10",
    )
    result = data["sources"]["ga4_organic"]
    assert result["window"]["startDate"] == "2026-01-01"
    assert result["comparison"]["status"] == "ok"
    assert result["comparison"]["window"]["startDate"] == "2025-01-01"
    assert result["comparison"]["applied"]["start_date"] == "2025-01-01"
    assert result["delta"] == {"sessions": 0.0}
    assert result["comparison"]["first_data_day"] == "2024-01-01"
    assert len(api_calls(live)) == 3


async def test_compare_two_pages_on_search_console(live):
    data = await ask(["search_pages"], page="/a", compare_page="/b")
    result = data["sources"]["search_pages"]
    assert result["applied"]["page"] == PAGE
    assert result["comparison"]["applied"]["page"] == OTHER
    expressions = {
        body["dimensionFilterGroups"][0]["filters"][0]["expression"] for body in reports(live)
    }
    assert expressions == {PAGE, OTHER}


async def test_failed_comparison_side_gives_no_delta(write_config, fake_net, account):
    write_config(SITE, account)

    def half(request):
        if request.url.host != "oauth2.googleapis.com":
            if body_of(request)["dateRanges"][0]["startDate"].startswith("2025"):
                return httpx2.Response(500)
        return answer(request)

    fake_net(half)
    data = await ask(
        ["ga4_organic"],
        start_date="2026-01-01",
        end_date=GOOD_DAY,
        compare_start_date="2025-01-01",
        compare_end_date="2025-01-10",
    )
    result = data["sources"]["ga4_organic"]
    assert result["status"] == "ok"
    assert result["comparison"]["status"] == "error"
    assert "delta" not in result


# Clarity: 10 calls for each project each day.
async def test_clarity_refuses_dates_and_a_query(live):
    data = await ask(["clarity"], start_date="2026-01-01", end_date=GOOD_DAY)
    assert data["sources"]["clarity"]["status"] == "unsupported"
    data = await ask(["clarity"], query="shoes")
    assert data["sources"]["clarity"]["status"] == "unsupported"
    assert not api_calls(live)


async def test_clarity_refuses_more_days_than_the_api_has(live):
    data = await ask(["clarity"], last_days=7)
    result = data["sources"]["clarity"]
    assert result["status"] == "unsupported"
    assert result["unsupported_params"] == ["last_days"]
    assert "1, 2, or 3" in result["reason"]
    assert not api_calls(live)


async def test_clarity_days_are_in_the_cache_key(live):
    first = await ask(["clarity"], last_days=1)
    second = await ask(["clarity"], last_days=2)
    third = await ask(["clarity"], last_days=1)
    assert [call.url.params["numOfDays"] for call in api_calls(live)] == ["1", "2"]
    assert first["sources"]["clarity"]["window"] == {"last_days": 1}
    assert second["sources"]["clarity"]["window"] == {"last_days": 2}
    assert third["sources"]["clarity"]["from_cache"] is True


async def test_clarity_page_filter_comes_before_rows_and_totals_and_costs_no_call(live):
    await ask(["clarity"])
    data = await ask(["clarity"], page="/a")
    result = data["sources"]["clarity"]
    assert len(api_calls(live)) == 1
    assert result["from_cache"] is True
    assert {row["Url"] for row in result["rows"]} == {PAGE, "http://www.example.com/a"}
    assert result["totals"] == {"sessions": 110}
    assert result["complete"] is True
    assert "page_match" in result


async def test_clarity_missing_page_is_not_a_zero(live):
    data = await ask(["clarity"], page="/none")
    result = data["sources"]["clarity"]
    assert result["rows"] == []
    assert result["thin_data"] is True
    assert result["totals"] == {"sessions": None}
    assert "not proof" in result["reason"]


async def test_clarity_full_answer_is_not_complete_for_a_page(write_config, fake_net, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    write_config(SITE)
    rows = [{"totalSessionCount": "1", "Url": f"{OTHER}/{n}"} for n in range(1000)]
    fake_net(
        lambda _: httpx2.Response(
            200,
            json=[{"metricName": "Traffic", "information": [*rows, {"Url": PAGE}]}],
        )
    )
    result = (await ask(["clarity"], page="/a"))["sources"]["clarity"]
    assert result["complete"] is False


def page_metric(name: str, count: int) -> dict[str, object]:
    rows = [{"totalSessionCount": "1", "Url": f"{OTHER}/{n}"} for n in range(count)]
    return {"metricName": name, "information": rows}


async def clarity_page(write_config, fake_net, monkeypatch, metrics) -> dict:
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    write_config(SITE)
    fake_net(lambda _: httpx2.Response(200, json=metrics))
    return (await ask(["clarity"], page="/a"))["sources"]["clarity"]


# The docs do not say if the limit counts each metric or the whole answer. The sum covers both.
async def test_clarity_metrics_at_the_row_limit_together_are_not_complete_for_a_page(
    write_config, fake_net, monkeypatch
):
    metrics = [page_metric("Traffic", 600), page_metric("Dead Click Count", 400)]
    result = await clarity_page(write_config, fake_net, monkeypatch, metrics)
    assert result["complete"] is False
    assert "Clarity gave 1,000 rows" in result["reason"]
    assert "last_days" in result["reason"]
    assert "not proof" in result["reason"]


async def test_clarity_metrics_below_the_row_limit_together_are_complete_for_a_page(
    write_config, fake_net, monkeypatch
):
    metrics = [page_metric("Traffic", 600), page_metric("Dead Click Count", 399)]
    result = await clarity_page(write_config, fake_net, monkeypatch, metrics)
    assert result["complete"] is True
    assert "Clarity gave" not in result["reason"]


async def test_clarity_compare_pages_spends_one_call(live):
    data = await ask(["clarity"], page="/a", compare_page="/b")
    assert len(api_calls(live)) == 1
    result = data["sources"]["clarity"]
    assert result["totals"] == {"sessions": 110}
    assert result["comparison"]["totals"] == {"sessions": 500}
    assert result["delta"] == {"sessions": -390}


async def test_clarity_project_field_keys_the_ledger(write_config, fake_net, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    write_config({**SITE, "clarity_project": "shared-project"})
    fake_net(lambda _: httpx2.Response(200, json=[TRAFFIC]))
    await ask(["clarity"])
    assert clarity.ledger_path("shared-project").exists()
    assert not clarity.ledger_path("www.example.com").exists()


async def test_clarity_stops_at_the_daily_budget(write_config, fake_net, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    write_config(SITE)
    calls = fake_net(lambda _: httpx2.Response(500))
    mark = shared.failure_path(clarity.cache_path("www.example.com", clarity_auth()))
    for _ in range(clarity.DAILY_CALLS):
        data = await ask(["clarity"])
        assert "HTTP 500" in data["sources"]["clarity"]["reason"]
        mark.unlink()
    data = await ask(["clarity"])
    result = data["sources"]["clarity"]
    assert result["status"] == "error"
    assert "10 calls" in result["reason"]
    assert len(calls) == clarity.DAILY_CALLS


async def test_clarity_cache_hit_spends_no_budget(live):
    await ask(["clarity"])
    await ask(["clarity"])
    stored = json.loads(clarity.ledger_path("www.example.com").read_text())
    assert stored["used"] == 1


async def test_clarity_ledger_of_an_older_day_starts_again(live):
    path = clarity.ledger_path("www.example.com")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"day": "2020-01-01", "used": 10}))
    assert (await ask(["clarity"]))["sources"]["clarity"]["status"] == "ok"


async def test_broken_clarity_ledger_fails_closed(live):
    path = clarity.ledger_path("www.example.com")
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    result = (await ask(["clarity"]))["sources"]["clarity"]
    assert result["status"] == "error"
    assert "ledger" in result["reason"]
    assert not api_calls(live)


async def test_parallel_clarity_runs_spend_one_call(live):
    await ask(["clarity"], page="/a", compare_page="/b")
    assert json.loads(clarity.ledger_path("www.example.com").read_text())["used"] == 1


# The guard: a source cannot claim a parameter and ignore it, and the list cannot drift.
PARAM_VALUES = {
    "dates": {"start_date": "2026-01-01", "end_date": GOOD_DAY},
    "last_days": {"last_days": 2},
    "page": {"page": "/a"},
    "query": {"query": "shoes"},
}
GROUPS = {
    "dates": {"start_date", "end_date"},
    "last_days": {"last_days"},
    "page": {"page"},
    "query": {"query"},
}


def test_every_source_declares_what_it_accepts():
    assert set(providers.ACCEPTS) == set(providers.SOURCES)
    known = {name for names in GROUPS.values() for name in names}
    for name, accepts in providers.ACCEPTS.items():
        assert accepts <= known, f"{name} declares an unknown parameter: {accepts - known}"


def trace(calls) -> list[tuple[str, str, bytes]]:
    return sorted((c.method, str(c.url), c.content) for c in api_calls(calls))


@pytest.mark.parametrize("group", list(GROUPS))
@pytest.mark.parametrize("name", sorted(providers.SOURCES))
async def test_a_declared_parameter_acts_and_an_undeclared_one_is_refused(live, name, group):
    plain = await ask([name])
    before = trace(live)
    live.clear()
    (cache := clarity.cache_path("www.example.com", clarity_auth())).unlink(missing_ok=True)
    assert not cache.exists()
    filtered = await ask([name], **PARAM_VALUES[group])
    after = trace(live)
    result = filtered["sources"][name]
    if GROUPS[group] <= providers.ACCEPTS[name]:
        assert result["status"] == "ok", result
        assert set(result["applied"]) == GROUPS[group]
        moved = after != before or result["rows"] != plain["sources"][name]["rows"]
        assert moved, f"{name} declares {group} but the parameter changed nothing"
    else:
        assert result["status"] == "unsupported", f"{name} ignored {group} in silence"
        assert not after


def test_tool_description_names_each_source_and_what_it_accepts():
    text = core.describe_sources()
    for name, accepts in providers.ACCEPTS.items():
        line = next(item for item in text.splitlines() if item.startswith(f"- {name}:"))
        for parameter in accepts:
            assert parameter in line
