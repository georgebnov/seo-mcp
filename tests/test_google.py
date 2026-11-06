import json

import httpx2

from seo_mcp import core
from seo_mcp.providers import _google

SITE = {"gsc_property": "sc-domain:example.com", "ga4_property_id": "123456789"}
ACCESS = "fake-access-token-1234"


def row(keys, clicks=1, impressions=10, ctr=0.1, position=5.0):
    return {
        "keys": keys,
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


def api(rows_by_dimensions=None, sitemap=None, inspect=None, ga4=None):
    rows_by_dimensions = rows_by_dimensions or {}

    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx2.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        if request.url.path.endswith("/searchAnalytics/query"):
            dimensions = tuple(json.loads(request.content).get("dimensions", ()))
            return httpx2.Response(200, json={"rows": rows_by_dimensions.get(dimensions, [])})
        if request.url.path.endswith("/sitemaps"):
            return httpx2.Response(200, json={"sitemap": sitemap or []})
        if request.url.path.endswith("index:inspect"):
            return inspect(json.loads(request.content))
        if request.url.path.endswith(":runReport"):
            return ga4(json.loads(request.content))
        return httpx2.Response(500)

    return handle


async def get(name):
    data = await core.get_seo_data("www.example.com", [name])
    return data["sources"][name]


async def test_search_queries_sort_and_totals(write_config, fake_net, account):
    write_config(SITE, account)
    rows = {
        ("query",): [row(["low"], 0, 5), row(["high"], 9, 200)],
        (): [row([], 9, 900)],
    }
    calls = fake_net(api(rows))
    result = await get("search_queries")
    assert result["status"] == "ok"
    assert result["rows"][0] == {
        "query": "high",
        "clicks": 9,
        "impressions": 200,
        "ctr": 0.1,
        "position": 5.0,
    }
    assert result["totals"]["impressions"] == 900
    assert result["thin_data"] is False
    assert result["window"]["end"] > result["window"]["start"]
    assert "sc-domain%3Aexample.com" in str(calls[-1].url)


async def test_few_impressions_are_thin(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(api({("page",): [row(["https://www.example.com/"], 0, 3)], (): [row([], 0, 3)]}))
    assert (await get("search_pages"))["thin_data"] is True


async def test_quick_wins_keep_positions_4_to_10_by_impressions(write_config, fake_net, account):
    write_config(SITE, account)
    rows = [
        row(["top", "/a"], position=2.0, impressions=900),
        row(["win", "/b"], position=7.0, impressions=300),
        row(["best", "/c"], position=4.0, impressions=500),
        row(["far", "/d"], position=30.0, impressions=800),
    ]
    fake_net(api({("query", "page"): rows, (): [row([], 0, 2000)]}))
    result = await get("quick_wins")
    assert [item["query"] for item in result["rows"]] == ["best", "win"]
    assert result["rows"][0]["page"] == "/c"


async def test_sitemaps(write_config, fake_net, account):
    write_config(SITE, account)
    entry = {
        "path": "https://www.example.com/sitemap.xml",
        "lastSubmitted": "2026-01-01",
        "isPending": False,
        "errors": "0",
        "warnings": "1",
        "contents": [{"submitted": "12"}, {"submitted": "3"}],
    }
    fake_net(api(sitemap=[entry]))
    result = await get("sitemaps")
    assert result["rows"][0]["submitted"] == 15
    assert result["rows"][0]["warnings"] == 1
    assert result["thin_data"] is False


async def test_no_sitemap_has_a_reason(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(api())
    result = await get("sitemaps")
    assert result["status"] == "ok"
    assert result["thin_data"] is True
    assert result["reason"]


def inspected(body):
    if body["inspectionUrl"].endswith("/bad"):
        return httpx2.Response(429)
    status = {
        "verdict": "PASS",
        "coverageState": "Submitted and indexed",
        "lastCrawlTime": "2026-01-01T00:00:00Z",
        "googleCanonical": body["inspectionUrl"],
        "userCanonical": body["inspectionUrl"],
    }
    return httpx2.Response(200, json={"inspectionResult": {"indexStatusResult": status}})


async def test_index_status_inspects_home_and_top_pages(write_config, fake_net, account):
    write_config(SITE, account)
    pages = [row(["https://www.example.com/a"], 3), row(["https://www.example.com/bad"], 2)]
    fake_net(api({("page",): pages}, inspect=inspected))
    result = await get("index_status")
    assert result["status"] == "ok"
    urls = [item["url"] for item in result["rows"]]
    assert urls == [
        "https://www.example.com/",
        "https://www.example.com/a",
        "https://www.example.com/bad",
    ]
    assert result["rows"][0]["coverage_state"] == "Submitted and indexed"
    assert result["rows"][0]["canonical_match"] is True
    assert result["rows"][2]["error"] == "HTTP 429"
    assert "1 of 3" in result["reason"]
    assert result["thin_data"] is True


async def test_index_status_all_failed_is_an_error(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(api(inspect=lambda _: httpx2.Response(403)))
    assert (await get("index_status"))["status"] == "error"


def report(body):
    dimension = body["dimensions"][0]["name"]
    assert body["dimensionFilter"]["filter"]["stringFilter"]["value"] == "Organic Search"
    values = {"date": ["20260102", "20260101"], "landingPage": ["/a", "/b"]}[dimension]
    return httpx2.Response(
        200,
        json={
            "dimensionHeaders": [{"name": dimension}],
            "metricHeaders": [{"name": "sessions"}, {"name": "totalUsers"}],
            "rows": [
                {
                    "dimensionValues": [{"value": value}],
                    "metricValues": [{"value": "150"}, {"value": "7"}],
                }
                for value in values
            ],
            "totals": [{"metricValues": [{"value": "300"}, {"value": "12"}]}],
        },
    )


async def test_ga4_organic(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(api(ga4=report))
    result = await get("ga4_organic")
    assert result["rows"][0] == {"date": "20260101", "sessions": 150.0, "totalUsers": 7.0}
    assert result["totals"] == {"sessions": 300.0, "totalUsers": 12.0}
    assert result["thin_data"] is False


async def test_ga4_landing_pages(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(api(ga4=report))
    result = await get("ga4_landing_pages")
    assert result["rows"][0]["landingPage"] == "/a"


async def test_ga4_empty_report_is_thin(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(
        api(ga4=lambda _: httpx2.Response(200, json={"metricHeaders": [{"name": "sessions"}]}))
    )
    result = await get("ga4_organic")
    assert result["status"] == "ok"
    assert result["thin_data"] is True
    assert result["reason"]


async def test_token_is_fetched_once_and_never_leaks(write_config, fake_net, account):
    write_config(SITE, account)
    calls = fake_net(api({("query",): [row(["q"])], ("page",): [row(["p"])], (): [row([])]}))
    data = await core.get_seo_data("www.example.com", ["search_queries"])
    await core.get_seo_data("www.example.com", ["search_pages"])
    assert sum(call.url.host == "oauth2.googleapis.com" for call in calls) == 1
    assert ACCESS not in json.dumps(data)


async def test_refused_sign_in_is_an_error_without_upstream_text(write_config, fake_net, account):
    write_config(SITE, account)
    fake_net(lambda _: httpx2.Response(400, text="grant-detail-from-google"))
    data = await core.get_seo_data("www.example.com", ["search_queries"])
    assert data["sources"]["search_queries"]["status"] == "error"
    assert "grant-detail" not in json.dumps(data)


async def test_no_account_is_not_connected(write_config):
    write_config(SITE)
    result = await get("ga4_organic")
    assert result["status"] == "not_connected"
    assert "google_service_account" in result["reason"]


async def test_bad_account_file_is_not_connected_and_silent(write_config, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"private_key": "pem-body-value"}')
    write_config(SITE, {"google_service_account": {"file": str(bad)}})
    data = await core.get_seo_data("www.example.com", ["search_queries"])
    assert data["sources"]["search_queries"]["status"] == "not_connected"
    assert "pem-body-value" not in json.dumps(data)


def test_scope_constants_are_read_only():
    assert _google.GSC_SCOPE.endswith(".readonly")
