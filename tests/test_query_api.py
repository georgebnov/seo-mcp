"""The full path: query_api sends 1 official read query and keeps the server read-only."""

import asyncio
import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from seo_mcp import api, core, providers, server
from seo_mcp.providers import bing, clarity, ga4

SITE = {
    "gsc_property": "sc-domain:example.com",
    "ga4_property_id": "123456789",
    "bing_site_url": "https://www.example.com/",
    "secrets": {"clarity_token": {"env": "CLARITY_ENV"}},
}
RANGE = {"startDate": "2026-01-01", "endDate": "2026-01-10"}
HOST = "www.example.com"
README = Path(__file__).parent.parent / "README.md"


def report(extra=None):
    return {
        "dimensionHeaders": [{"name": "pagePath"}, {"name": "dateRange"}],
        "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
        "rows": [
            {
                "dimensionValues": [{"value": "/a"}, {"value": "date_range_0"}],
                "metricValues": [{"value": "7"}],
            }
        ],
        "rowCount": 5,
        "metadata": {"subjectToThresholding": True},
        **(extra or {}),
    }


def ga4_kind(request):
    """The docs: kind is always analyticsData# and the name of the method."""
    return {"kind": f"analyticsData#{request.url.path.rsplit(':', 1)[-1]}"}


def answer(request):
    host, path = request.url.host, request.url.path
    if host == "oauth2.googleapis.com":
        return httpx2.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
    if path.endswith("/searchAnalytics/query"):
        keys = [f"key-{name}" for name in json.loads(request.content).get("dimensions", [])]
        row = {"keys": keys, "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}
        return httpx2.Response(200, json={"rows": [row]})
    if path.endswith("index:inspect"):
        return httpx2.Response(200, json={"inspectionResult": {"inspectionResultLink": "x"}})
    if "/sitemaps" in path:
        return httpx2.Response(200, json={"sitemap": [{"path": "s"}], "path": "s"})
    if path.endswith("/metadata"):
        return httpx2.Response(
            200,
            json={
                "dimensions": [
                    {"apiName": "pagePath", "uiName": "Page path", "category": "Page / screen"},
                    {"apiName": "customEvent:plan", "customDefinition": True},
                ],
                "metrics": [{"apiName": "sessions", "description": "long text"}],
            },
        )
    if path.endswith(("runReport", "runPivotReport", "runRealtimeReport")):
        return httpx2.Response(200, json=report(ga4_kind(request)))
    if path.endswith("runPagespeed"):
        audits = {"lcp": {"title": "LCP", "score": 0.9, "numericValue": 1, "details": {"x": 1}}}
        lighthouse = {"categories": {"seo": {"score": 1}}, "audits": audits, "fetchTime": "t"}
        return httpx2.Response(200, json={"lighthouseResult": lighthouse})
    if "records:" in path:
        record = {"metrics": {"lcp": {"percentiles": {"p75": 1}}}, "key": {"origin": "o"}}
        return httpx2.Response(200, json={"record": record})
    if host == "ssl.bing.com":
        date = "/Date(86400000-0700)/"
        return httpx2.Response(200, json={"d": [{"Query": "q", "Date": date, "Clicks": 1}]})
    if host == "www.clarity.ms":
        return httpx2.Response(
            200, json=[{"metricName": "Traffic", "information": [{"OS": "iOS"}, {"OS": "Linux"}]}]
        )
    if path.endswith("/mcp/dashboard/query"):
        row = {"Device": "PC", "SessionCount": 1}
        return httpx2.Response(200, json={"query": "q", "data": [row], "dataErrorType": 0})
    if host == "clarity.microsoft.com":
        row = {
            "sessionId": "s1",
            "timestamp": "2026-01-01 10:00:00",
            "link": "https://clarity.microsoft.com/player/p/s1",
        }
        return httpx2.Response(200, json=[row])
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


def sent(calls):
    return [call for call in calls if call.url.host != "oauth2.googleapis.com"]


async def ask(name, method, request=None):
    return await core.query_api(HOST, name, method, request)


# An agent finds what it can ask for without a network call.
async def test_catalog_lists_each_api_and_method(live):
    data = await core.query_api()
    assert data["status"] == "ok"
    names = {row["api"]: row for row in data["rows"]}
    assert set(names) == set(providers.APIS)
    assert "run_report" in names["ga4"]["methods"]
    assert not live


async def test_options_list_fields_values_and_gaps(live):
    data = await core.query_api(api="search_console")
    assert "query" in data["options"]["dimensions"]
    assert "includingRegex" in data["options"]["operators"]
    assert data["docs"].startswith("https://")
    method = next(row for row in data["rows"] if row["method"] == "search_analytics")
    assert "dimensionFilterGroups" in method["fields"]
    assert data["not_in_the_api"]
    assert not live


@pytest.mark.parametrize("name", sorted(providers.APIS))
def test_each_api_has_docs_and_a_list_of_gaps_in_the_readme(name):
    entry = providers.APIS[name]
    assert entry.docs.startswith("https://")
    assert entry.methods
    readme = README.read_text()
    for gap in entry.gaps:
        assert gap in readme, f"README has no line for this gap of {name}: {gap}"


async def test_ga4_metadata_gives_the_live_names_of_the_property(live):
    data = await ask("ga4", "metadata")
    assert sent(live)[0].url.path == "/v1beta/properties/123456789/metadata"
    assert {"kind": "metric", "apiName": "sessions"} in data["rows"]
    custom = next(row for row in data["rows"] if row["apiName"] == "customEvent:plan")
    assert custom["custom"] is True


async def test_unknown_api_and_method_name_the_known_ones(live):
    data = await core.query_api(HOST, "nope", "x")
    assert data["status"] == "error"
    assert "search_console" in data["reason"]
    data = await ask("ga4", "nope")
    assert data["status"] == "error"
    assert "run_report" in data["reason"]


# A field of a request is never dropped in silence.
@pytest.mark.parametrize(
    ("name", "method"),
    [(name, method) for name, entry in providers.APIS.items() for method in entry.methods],
)
async def test_each_method_refuses_an_unknown_field_before_the_network(live, name, method):
    data = await ask(name, method, {"madeUpField": 1})
    assert data["status"] == "error"
    assert "madeUpField" in data["reason"]
    assert not live


@pytest.mark.parametrize(
    ("name", "method", "field"),
    [
        ("search_console", "search_analytics", "siteUrl"),
        ("search_console", "url_inspection", "siteUrl"),
        ("ga4", "run_report", "property"),
        ("bing", "GetQueryStats", "siteUrl"),
        ("bing", "GetQueryStats", "apikey"),
    ],
)
async def test_a_field_of_the_server_is_refused(live, name, method, field):
    data = await ask(name, method, {field: "https://www.evil.test/"})
    assert data["status"] == "error"
    assert not live


# Search Console.
async def test_search_analytics_sends_the_full_query(live):
    request = {
        **RANGE,
        "dimensions": ["query", "page", "country", "device", "date"],
        "type": "image",
        "dimensionFilterGroups": [
            {
                "groupType": "and",
                "filters": [
                    {"dimension": "country", "operator": "equals", "expression": "can"},
                    {"dimension": "page", "operator": "includingRegex", "expression": "/blog/.*"},
                ],
            }
        ],
        "aggregationType": "byPage",
        "rowLimit": 50,
        "startRow": 100,
        "dataState": "all",
    }
    data = await ask("search_console", "search_analytics", request)
    assert json.loads(sent(live)[0].content) == request
    assert data["rows"][0]["query"] == "key-query"
    assert data["rows"][0]["clicks"] == 1
    assert data["applied"] == request
    assert data["more_rows"] == "no"


async def test_search_analytics_full_page_may_have_more_rows(live):
    data = await ask("search_console", "search_analytics", {**RANGE, "rowLimit": 1})
    assert data["more_rows"] == "possible"
    assert data["row_count"] is None


async def test_search_analytics_default_limit_is_shown(live):
    data = await ask("search_console", "search_analytics", RANGE)
    assert json.loads(sent(live)[0].content)["rowLimit"] == api.MAX_ROWS
    assert data["applied"]["rowLimit"] == api.MAX_ROWS


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"startDate": "2026-1-1"}, "startDate"),
        ({"startDate": "2026-02-01"}, "startDate"),
        ({"dimensions": ["nope"]}, "dimensions"),
        ({"type": "nope"}, "type"),
        ({"aggregationType": "nope"}, "aggregationType"),
        ({"dataState": "nope"}, "dataState"),
        ({"rowLimit": 1001}, "startRow"),
        ({"rowLimit": 0}, "rowLimit"),
        ({"rowLimit": "x"}, "rowLimit"),
        ({"startRow": -1}, "startRow"),
        ({"dimensionFilterGroups": "x"}, "dimensionFilterGroups"),
        ({"dimensionFilterGroups": [{"groupType": "or", "filters": []}]}, "groupType"),
        ({"dimensionFilterGroups": [{"filters": [{"dimension": "x"}]}]}, "dimension"),
        (
            {
                "dimensionFilterGroups": [
                    {"filters": [{"dimension": "page", "operator": "x", "expression": "a"}]}
                ]
            },
            "operator",
        ),
        (
            {
                "dimensionFilterGroups": [
                    {"filters": [{"dimension": "page", "expression": "a" * 4097}]}
                ]
            },
            "expression",
        ),
        (
            {"dimensionFilterGroups": [{"filters": [{"dimension": "page", "extra": 1}]}]},
            "extra",
        ),
    ],
)
async def test_search_analytics_checks_nested_values_before_the_network(live, change, named):
    data = await ask("search_console", "search_analytics", {**RANGE, **change})
    assert data["status"] == "error"
    assert named in data["reason"]
    assert not live


@pytest.mark.parametrize(
    ("name", "method", "request_body", "named"),
    [
        (
            "search_console",
            "search_analytics",
            {"startDate": 5, "endDate": "2026-01-02"},
            "startDate",
        ),
        ("search_console", "url_inspection", {"inspectionUrl": 5}, "inspectionUrl"),
        ("pagespeed", "run", {"url": ["/a"]}, "url"),
        ("bing", "GetUrlInfo", {"url": None}, "url"),
    ],
)
async def test_a_value_of_the_wrong_type_gets_a_clear_reason(
    live, name, method, request_body, named
):
    data = await ask(name, method, request_body)
    assert data["status"] == "error"
    assert named in data["reason"]
    assert "TypeError" not in data["reason"]
    assert not live


async def test_search_analytics_requires_the_dates(live):
    data = await ask("search_console", "search_analytics", {})
    assert data["status"] == "error"
    assert "startDate" in data["reason"]


async def test_url_inspection_is_on_the_site_only(live):
    data = await ask("search_console", "url_inspection", {"inspectionUrl": "/a"})
    assert json.loads(sent(live)[0].content) == {
        "inspectionUrl": "https://www.example.com/a",
        "siteUrl": "sc-domain:example.com",
    }
    assert data["rows"][0]["inspectionResultLink"] == "x"
    live.clear()
    data = await ask("search_console", "url_inspection", {"inspectionUrl": "https://evil.test/a"})
    assert data["status"] == "error"
    assert not live


async def test_sitemap_path_cannot_leave_the_endpoint(live):
    data = await ask("search_console", "sitemap", {"feedpath": "/sitemap.xml"})
    assert data["status"] == "ok"
    assert sent(live)[0].url.raw_path.endswith(
        b"/sitemaps/https%3A%2F%2Fwww.example.com%2Fsitemap.xml"
    )


# GA4.
async def test_run_report_sends_2_periods_and_flattens_rows(live):
    request = {
        "dateRanges": [
            RANGE,
            {"startDate": "2025-01-01", "endDate": "2025-01-10", "name": "last_year"},
        ],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "sessions"}],
        "dimensionFilter": {"filter": {"fieldName": "pagePath", "stringFilter": {"value": "/a"}}},
        "offset": 1,
    }
    data = await ask("ga4", "run_report", request)
    body = json.loads(sent(live)[0].content)
    assert body == {**request, "limit": api.MAX_ROWS, "returnPropertyQuota": True}
    assert data["rows"] == [{"pagePath": "/a", "dateRange": "date_range_0", "sessions": 7.0}]
    assert data["row_count"] == 5
    assert data["more_rows"] == "yes"
    assert data["metadata"]["subjectToThresholding"] is True


async def test_ga4_limit_over_the_cap_names_offset(live):
    data = await ask("ga4", "run_report", {"limit": 5000})
    assert data["status"] == "error"
    assert "offset" in data["reason"]
    assert not live


@pytest.mark.parametrize(
    ("method", "path"),
    [("pivot_report", ":runPivotReport"), ("realtime_report", ":runRealtimeReport")],
)
async def test_other_ga4_reports(live, method, path):
    data = await ask("ga4", method, {"metrics": [{"name": "sessions"}]})
    assert data["status"] == "ok"
    assert sent(live)[0].url.path.endswith(path)


# The API says why it refused a query, and the text stays safe.
def refuse(text):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx2.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        return httpx2.Response(400, text=text)

    return handle


async def test_api_validation_error_comes_back_as_the_reason(live, fake_net):
    message = "Field madeUpMetric is not a valid metric."
    fake_net(refuse(json.dumps({"error": {"code": 400, "message": message}})))
    data = await ask("ga4", "run_report", {"metrics": [{"name": "madeUpMetric"}]})
    assert data["status"] == "error"
    assert message in data["reason"]
    assert "HTTP 400" in data["reason"]


async def test_api_message_is_scrubbed_then_cut(live, fake_net):
    message = "bad key=abc123secret token fake-token " + "x" * 2000
    fake_net(refuse(json.dumps({"error": {"message": message}})))
    await core.get_seo_data(HOST, ["ga4_organic"])
    data = await ask("ga4", "run_report", {"metrics": []})
    assert "abc123secret" not in data["reason"]
    assert "fake-token" not in data["reason"]
    assert len(data["reason"]) <= 400


@pytest.mark.parametrize(
    "text",
    [
        "plain upstream body",
        json.dumps({"error": "invalid_grant"}),
        json.dumps({"error": {"message": 5}}),
        json.dumps(["x"]),
    ],
)
async def test_other_error_bodies_stay_out(live, fake_net, text):
    fake_net(refuse(text))
    data = await ask("ga4", "run_report", {"metrics": []})
    assert data["status"] == "error"
    assert data["reason"].startswith("HTTP 400")
    assert "upstream" not in data["reason"]
    assert "invalid_grant" not in data["reason"]


async def test_the_simple_path_keeps_fixed_text(live, fake_net):
    fake_net(refuse(json.dumps({"error": {"message": "upstream-detail"}})))
    data = await core.get_seo_data(HOST, ["ga4_organic"])
    assert "upstream-detail" not in json.dumps(data)


async def test_bing_error_text_stays_out(live, fake_net):
    fake_net(lambda request: httpx2.Response(400, text=str(request.url)))
    data = await ask("bing", "GetQueryStats")
    assert "bing-key-value" not in json.dumps(data)
    assert data["status"] == "error"


# PageSpeed and the Chrome UX Report.
async def test_pagespeed_full_query(live):
    request = {
        "url": "/a",
        "strategy": "DESKTOP",
        "category": ["SEO", "PERFORMANCE"],
        "locale": "fr",
    }
    data = await ask("pagespeed", "run", request)
    params = sent(live)[0].url.params
    assert params["url"] == "https://www.example.com/a"
    assert params["strategy"] == "DESKTOP"
    assert params.get_list("category") == ["SEO", "PERFORMANCE"]
    assert params["locale"] == "fr"
    assert data["rows"][0]["audit"] == "lcp"
    assert data["scores"] == {"seo": 100}


async def test_pagespeed_refuses_a_bad_strategy_and_another_host(live):
    assert (await ask("pagespeed", "run", {"strategy": "x"}))["status"] == "error"
    assert (await ask("pagespeed", "run", {"url": "https://evil.test/"}))["status"] == "error"
    assert not live


async def test_crux_history_and_record(live):
    request = {"url": "/a", "formFactor": "PHONE", "metrics": ["lcp"], "collectionPeriodCount": 40}
    data = await ask("crux", "history", request)
    assert sent(live)[0].url.path == "/v1/records:queryHistoryRecord"
    assert json.loads(sent(live)[0].content) == {**request, "url": "https://www.example.com/a"}
    assert data["rows"][0]["metric"] == "lcp"
    data = await ask("crux", "record", {})
    assert json.loads(sent(live)[1].content) == {"origin": "https://www.example.com"}


@pytest.mark.parametrize(
    "request_body",
    [
        {"origin": "https://www.evil.test"},
        {"origin": "https://www.example.com", "url": "/a"},
        {"formFactor": "WATCH"},
    ],
)
async def test_crux_refuses_bad_input(live, request_body):
    assert (await ask("crux", "record", request_body))["status"] == "error"
    assert not live


async def test_crux_period_count_has_the_api_limit(live):
    data = await ask("crux", "history", {"collectionPeriodCount": 41})
    assert "collectionPeriodCount" in data["reason"]
    assert (await ask("crux", "record", {"collectionPeriodCount": 2}))["status"] == "error"
    assert not live


async def test_crux_no_data_is_ok_and_says_so(live, fake_net):
    fake_net(lambda _: httpx2.Response(404))
    data = await ask("crux", "record", {"url": "/a"})
    assert data["status"] == "ok"
    assert data["thin_data"] is True
    assert data["reason"]


# Bing.
async def test_bing_method_fills_the_site_and_quotes_only_query_text(live):
    data = await ask("bing", "GetQueryPageDetailStats", {"query": "shoes", "page": "/a"})
    params = sent(live)[0].url.params
    assert params["siteUrl"] == "https://www.example.com/"
    assert params["query"] == '"shoes"'
    assert params["page"] == "https://www.example.com/a"
    assert data["rows"][0]["Date"] == "1970-01-02"
    assert "bing-key" not in json.dumps(data)


async def test_bing_page_index_stays_a_number(live):
    await ask("bing", "GetUrlLinks", {"link": "/a", "page": 2})
    params = sent(live)[0].url.params
    assert params["page"] == "2"
    assert params["link"] == "https://www.example.com/a"


async def test_bing_arguments_are_required_and_on_the_site(live):
    assert (await ask("bing", "GetPageQueryStats"))["status"] == "error"
    data = await ask("bing", "GetPageQueryStats", {"page": "https://evil.test/"})
    assert data["status"] == "error"
    assert not live


def test_each_bing_method_is_a_listed_get_endpoint():
    paths = {e.path for e in bing.ENDPOINTS}
    for name in bing.METHODS:
        assert name.startswith("Get")
        assert rf"/webmaster/api\.svc/json/{name}" in paths


# Clarity: every path spends the same daily budget.
async def test_live_insights_dimensions_are_in_the_cache_key(live):
    first = await ask("clarity", "live_insights", {"numOfDays": 1, "dimension1": "OS"})
    second = await ask(
        "clarity", "live_insights", {"numOfDays": 1, "dimension1": "OS", "dimension2": "Browser"}
    )
    third = await ask("clarity", "live_insights", {"numOfDays": 1, "dimension1": "OS"})
    assert len(sent(live)) == 2
    assert sent(live)[1].url.params["dimension2"] == "Browser"
    assert first["from_cache"] is False
    assert second["from_cache"] is False
    assert third["from_cache"] is True
    assert third["calls_used_today"] == 2
    assert first["rows"][0] == {"metric": "Traffic", "OS": "iOS"}


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"numOfDays": 4}, "numOfDays"),
        ({"numOfDays": "2"}, "numOfDays"),
        ({"numOfDays": 1, "dimension1": "Page"}, "dimension1"),
        ({"numOfDays": 1, "dimension2": "OS"}, "dimension1"),
    ],
)
async def test_live_insights_checks_before_it_spends(live, change, named):
    data = await ask("clarity", "live_insights", change)
    assert data["status"] == "error"
    assert named in data["reason"]
    assert not live
    assert not clarity.ledger_path(HOST).exists()


DAY = {"start": "2026-01-01T00:00:00.000Z", "end": "2026-01-02T00:00:00.000Z"}


async def test_recordings_send_filters_and_the_sort_number(live):
    filters = {"date": DAY, "deviceType": ["Mobile"], "rageClickPresent": True}
    request = {"filters": filters, "sortBy": "SessionDuration_DESC", "count": 20}
    data = await ask("clarity", "recordings", request)
    call = sent(live)[0]
    assert call.url.host == "clarity.microsoft.com"
    assert call.url.path == "/mcp/recordings/sample"
    assert json.loads(call.content) == {
        "sortBy": 3,
        "start": DAY["start"],
        "end": DAY["end"],
        "filters": filters,
        "count": 20,
    }
    assert call.headers["authorization"].startswith("Bearer ")
    assert data["rows"] == [
        {"timestamp": "2026-01-01 10:00:00", "link": "https://clarity.microsoft.com/player/p/s1"}
    ]
    assert data["calls_used_today"] == 1


async def test_recordings_same_request_is_cached_and_another_is_not(live):
    request = {"filters": {"date": DAY}}
    await ask("clarity", "recordings", request)
    second = await ask("clarity", "recordings", request)
    await ask("clarity", "recordings", {**request, "count": 5})
    assert second["from_cache"] is True
    assert len(sent(live)) == 2


@pytest.mark.parametrize(
    ("request_body", "named"),
    [
        ({}, "filters"),
        ({"filters": {}}, "date"),
        ({"filters": {"date": {"start": "2026-01-01"}}}, "date"),
        ({"filters": {"date": {**DAY, "start": "yesterday"}}}, "date"),
        ({"filters": {"date": {"start": DAY["end"], "end": DAY["start"]}}}, "date"),
        ({"filters": {"date": DAY, "madeUp": 1}}, "madeUp"),
        ({"filters": {"date": DAY}, "count": 251}, "count"),
        ({"filters": {"date": DAY}, "sortBy": "Newest"}, "sortBy"),
    ],
)
async def test_recordings_check_before_they_spend(live, request_body, named):
    data = await ask("clarity", "recordings", request_body)
    assert data["status"] == "error"
    assert named in data["reason"]
    assert not live


async def test_dashboard_query_sends_text_and_time_zone(live):
    data = await ask("clarity", "dashboard_query", {"query": "Top browsers last 3 days"})
    assert sent(live)[0].url.path == "/mcp/dashboard/query"
    assert json.loads(sent(live)[0].content) == {
        "query": "Top browsers last 3 days",
        "timezone": "UTC",
    }
    assert data["status"] == "ok"


async def test_dashboard_rows_come_from_the_data_list(live, fake_net):
    reply = {"query": "q", "data": [{"Device": "PC"}, {"Device": "Mobile"}], "dataErrorType": 0}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["rows"] == [{"Device": "PC"}, {"Device": "Mobile"}]
    assert data["reason"] is None


async def test_dashboard_error_type_is_not_an_empty_success(live, fake_net):
    reply = {"query": "q", "data": [], "dataErrorType": 3}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["thin_data"] is True
    assert "3" in data["reason"]


async def test_all_clarity_paths_share_1_budget(live, write_config):
    project = clarity.ledger_path(HOST)
    project.parent.mkdir(parents=True)
    today = datetime.now(UTC).date().isoformat()
    project.write_text(json.dumps({"day": today, "used": clarity.DAILY_CALLS}))
    for method, request in (
        ("live_insights", {"numOfDays": 1}),
        ("recordings", {"filters": {"date": DAY}}),
        ("dashboard_query", {"query": "q"}),
    ):
        data = await ask("clarity", method, request)
        assert data["status"] == "error"
        assert "10 calls" in data["reason"]
    assert not live


# Parity with the schema of the official Clarity MCP server.
@pytest.mark.parametrize(
    ("filters", "named"),
    [
        ({"userType": "Robot"}, "userType"),
        ({"sessionIntent": ["High Intention"]}, "sessionIntent"),
        ({"deviceType": ["Fridge"]}, "deviceType"),
        ({"deviceType": "Mobile"}, "deviceType"),
        ({"browser": ["Netscape"]}, "browser"),
        ({"os": ["BeOS"]}, "os"),
        ({"channel": ["Radio"]}, "channel"),
        ({"visitedUrls": ["/a"]}, "visitedUrls"),
        ({"entryUrls": [{"url": "/a", "operator": "like"}]}, "entryUrls"),
        ({"exitUrls": [{"operator": "contains"}]}, "exitUrls"),
        ({"country": "Canada"}, "country"),
        ({"deadClickPresent": "yes"}, "deadClickPresent"),
        ({"clickedText": 5}, "clickedText"),
        ({"pageDuration": 5}, "pageDuration"),
        ({"pageDuration": {"min": 1}}, "pageDuration"),
        ({"scrollDepth": {"min": 0, "max": 101}}, "scrollDepth"),
        ({"performanceScore": {"min": -1, "max": None}}, "performanceScore"),
    ],
)
async def test_a_bad_filter_value_spends_no_call(live, filters, named):
    data = await ask("clarity", "recordings", {"filters": {"date": DAY, **filters}})
    assert data["status"] == "error"
    assert named in data["reason"]
    assert not live


async def test_each_kind_of_filter_goes_out_unchanged(live):
    filters = {
        "date": DAY,
        "userType": "NewUser",
        "sessionIntent": "High Intention",
        "visitedUrls": [{"url": "/a", "operator": "contains"}],
        "country": ["Canada"],
        "browser": ["Chrome"],
        "channel": ["AITools"],
        "smartEvents": ["SignUp", "my own event"],
        "javascriptErrors": [""],
        "clickedText": "Buy",
        "quickbackClickPresent": True,
        "pageDuration": {"min": 1, "max": None},
        "scrollDepth": {"min": 0, "max": 100},
    }
    data = await ask("clarity", "recordings", {"filters": filters})
    assert data["status"] == "ok"
    assert json.loads(sent(live)[0].content)["filters"] == filters


async def test_clarity_options_list_the_valid_filter_values(live):
    options = (await ask("clarity", ""))["options"]
    values = options["recording_filter_values"]
    assert "Tablet" in values["deviceType"]
    assert "PaidAITools" in values["channel"]
    assert "matchesRegex" in options["url_operators"]
    assert "SignUp" in options["smart_event_examples"]
    assert set(options["recording_filter_units"]) >= {"pageDuration", "firstInputDelay"}
    assert any("1 metric" in rule for rule in options["dashboard_rules"])


async def test_recordings_say_that_they_are_a_sample(live):
    data = await ask("clarity", "recordings", {"filters": {"date": DAY}, "count": 250})
    assert data["more_rows"] == "possible"
    assert "sample" in data["sample"]


async def test_dashboard_shows_the_question_that_clarity_answered(live, fake_net):
    reply = {"query": "Rewritten question", "data": [{"Device": "PC"}], "dataErrorType": 0}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["answered_question"] == "Rewritten question"


# The wording of a real answer that Clarity rewrote, with the dates changed.
TOP_10 = (
    "Total dead click counts per visited page URL for non-bot users between "
    "'2026/08/12 00:00:00' and '2026/11/09 23:59:59', showing the top 10 pages by dead clicks."
)


def dead_click_pages(count: int) -> list[dict[str, object]]:
    return [
        {"VisitedUrl": f"https://www.example.com/{n}", "TotalDeadClicks": 9 - n}
        for n in range(count)
    ]


async def test_a_full_top_list_of_the_dashboard_can_hide_rows(live, fake_net):
    reply = {"query": TOP_10, "data": dead_click_pages(10), "dataErrorType": 0}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["more_rows"] == "possible"
    assert "top 10" in data["reason"]
    assert "full URL" in data["reason"]


async def test_a_short_top_list_of_the_dashboard_has_all_rows(live, fake_net):
    reply = {"query": TOP_10, "data": dead_click_pages(7), "dataErrorType": 0}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["more_rows"] == "no"
    assert data["reason"] is None


async def test_a_dashboard_answer_with_no_top_list_has_all_rows(live, fake_net):
    question = "Dead click counts per page between '2026/08/12' and '2026/11/09'."
    reply = {"query": question, "data": dead_click_pages(10), "dataErrorType": 0}
    fake_net(lambda _: httpx2.Response(200, json=reply))
    data = await ask("clarity", "dashboard_query", {"query": "q"})
    assert data["more_rows"] == "no"
    assert data["reason"] is None


async def test_a_removed_method_is_unknown_and_spends_nothing(live):
    data = await ask("clarity", "documentation", {"query": "q"})
    assert data["status"] == "error"
    assert "Unknown method" in data["reason"]
    assert not live
    assert not clarity.ledger_path(HOST).exists()


async def test_the_catalog_lists_only_calls_that_work():
    options = await core.query_api(api="clarity")
    assert [row["method"] for row in options["rows"]] == [
        "live_insights",
        "recordings",
        "dashboard_query",
    ]
    assert "documentation" not in json.dumps(options)


# The size of a result that goes into a model context.
def test_answer_cuts_rows_at_the_byte_cap_and_says_so():
    rows = [{"text": "x" * 1000} for _ in range(500)]
    result = api.answer(rows, more_rows="no")
    assert len(json.dumps(result)) <= api.MAX_BYTES + 1000
    assert result["truncated"] is True
    assert result["rows_returned"] == len(result["rows"]) < 500
    assert result["more_rows"] == "yes"


def test_answer_cuts_rows_at_the_row_cap():
    result = api.answer([{"a": 1}] * 1200, more_rows="no")
    assert result["rows_returned"] == api.MAX_ROWS
    assert result["truncated"] is True


async def test_no_site_entry_is_a_clear_error(live):
    data = await core.query_api("www.nope.test", "ga4", "metadata")
    assert data["status"] == "error"
    assert not live


async def test_not_connected_field(write_config, fake_net):
    write_config()
    data = await ask("ga4", "metadata")
    assert data["status"] == "not_connected"


async def test_result_is_scrubbed(live, fake_net):
    def leaky(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx2.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        row = {"keys": ["https://x.test/?apikey=abc123secret"], "clicks": 0}
        row |= {"impressions": 0, "ctr": 0, "position": 0}
        return httpx2.Response(200, json={"rows": [row]})

    fake_net(leaky)
    data = await ask("search_console", "search_analytics", {**RANGE, "dimensions": ["page"]})
    assert "abc123secret" not in json.dumps(data)


# Parallel agents: each session has its own server process, and all share 1 cache folder.
async def test_parallel_calls_on_a_cold_cache_spend_1_call(live):
    request = {"filters": {"date": DAY}}
    results = await asyncio.gather(*(ask("clarity", "recordings", request) for _ in range(5)))
    assert len(sent(live)) == 1
    assert sum(result["from_cache"] is False for result in results) == 1
    assert json.loads(clarity.ledger_path(HOST).read_text())["used"] == 1


async def test_a_call_waits_for_another_process_that_fetches_the_same_answer(live):
    path = clarity.cache_path(HOST)
    path.parent.mkdir(parents=True)
    with path.with_suffix(".lock").open("a") as other_process:
        fcntl.flock(other_process, fcntl.LOCK_EX)
        waiting = asyncio.create_task(core.get_seo_data(HOST, ["clarity"]))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        path.write_text(json.dumps({"cached_at": datetime.now(UTC).isoformat(), "answer": []}))
    data = await waiting
    assert data["sources"]["clarity"]["from_cache"] is True
    assert not sent(live)
    assert not clarity.ledger_path(HOST).exists()


# George: an agent read a lower position, or a position delta, as good or bad. The average
# moves when new queries appear, so each tool that returns position says what it means.
POSITION_MEANS = (
    "The average highest place of your link across all queries with impressions. A lower "
    "number is not always better, and it moves when new queries appear."
)
POSITION_CHANGE = (
    "A position change alone is not good or bad. Judge the change by clicks first, then by "
    "the click rate and impressions of the same rows."
)


@pytest.mark.parametrize("name", ["get_seo_data", "query_api"])
async def test_each_tool_that_returns_position_says_what_it_means(name):
    tools = {tool.name: tool for tool in await server.server.list_tools()}
    flat = " ".join(tools[name].description.split())
    assert f"Search Console position: {POSITION_MEANS} {POSITION_CHANGE}" in flat


def test_tool_description_asks_for_named_sources():
    assert "Name the sources that you need" in server.GET_SEO_DATA
    assert "Leave it out" not in server.GET_SEO_DATA


# An agent reads the choices at session start and goes direct to query_api(api="<name>").
def test_tool_description_names_each_api_with_1_line():
    for name, entry in providers.APIS.items():
        assert entry.summary.strip()
        assert f"\n- {name}: {entry.summary}\n" in server.QUERY_API


async def ga4_options():
    return await core.query_api(api="ga4")


async def test_ga4_options_hold_the_standard_names():
    options = (await ga4_options())["options"]
    assert {"sessionDefaultChannelGroup", "landingPage", "pagePath"} <= set(options["dimensions"])
    assert {"sessions", "totalUsers", "keyEvents"} <= set(options["metrics"])
    assert len(options["dimensions"]) == len(set(options["dimensions"]))
    assert len(options["metrics"]) == len(set(options["metrics"]))


async def test_ga4_options_say_when_metadata_is_needed_and_the_date_of_the_list():
    rule = (await ga4_options())["options"]["dimensions_and_metrics"]
    assert "metadata only for custom names or for a name that this list does not hold" in rule
    assert ga4.NAMES_TAKEN_ON in rule
    assert ga4.SCHEMA_DOCS in rule


async def test_the_ga4_options_fit_in_1_answer():
    data = await ga4_options()
    assert len(json.dumps(data)) < api.MAX_BYTES
    assert data["truncated"] is False


def test_the_standard_ga4_names_stay_out_of_the_tool_text():
    text = server.QUERY_API + core.describe_apis()
    names = ga4.STANDARD_DIMENSIONS + ga4.STANDARD_METRICS
    leaked = [name for name in names if len(name) > 12 and name in text]
    assert not leaked, f"Keep the table out of the tool text: {leaked}"


def test_each_ga4_name_of_the_server_is_a_standard_name():
    dimensions = {ga4.ORGANIC["filter"]["fieldName"], "date", "landingPage"}
    missing = sorted(set(ga4.METRICS) - set(ga4.STANDARD_METRICS))
    missing += sorted(dimensions - set(ga4.STANDARD_DIMENSIONS))
    assert not missing, f"Take these names from {ga4.SCHEMA_DOCS} into ga4.py: {missing}"
