"""An agent sees the quota before it spends, and each answer that spends shows the count."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, net, providers, quota
from seo_mcp.providers import clarity, ga4, gsc
from test_query_api import DAY, HOST, SITE, answer

INSIGHTS = ("live_insights", {"numOfDays": 1})
RECORDINGS = ("recordings", {"filters": {"date": DAY}})
DASHBOARD = ("dashboard_query", {"query": "q"})
INSPECT = ("url_inspection", {"inspectionUrl": f"https://{HOST}/a"})
TODAY = datetime.now(UTC).date().isoformat()
GA4_QUOTA = {
    "tokensPerDay": {"consumed": 12, "remaining": 199_000},
    "tokensPerHour": {"consumed": 12, "remaining": 39_000},
    "tokensPerProjectPerHour": {"consumed": 12, "remaining": 13_000},
}


def sent(calls):
    return [call for call in calls if call.url.host != "oauth2.googleapis.com"]


def set_ledger(ledger, key, **body):
    path = quota.path(ledger, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": TODAY, **body}))


def run(api, call):
    return core.query_api(HOST, api, call[0], call[1])


def replies(handler):
    def reply(request):
        return handler(request) or answer(request)

    return reply


# Each answer that spends shows the count.
@pytest.mark.parametrize("call", [INSIGHTS, RECORDINGS, DASHBOARD])
async def test_each_clarity_method_shows_the_count(live, call):
    data = await run("clarity", call)
    assert data["status"] == "ok"
    assert data["calls_used_today"] == 1
    assert data["daily_limit"] == 10
    assert data["calls_left"] == 9
    assert data["reset_stated"] is False
    assert data["resets_at"].endswith("T00:00:00+00:00")


async def test_the_clarity_source_shows_the_count(live):
    data = await core.get_seo_data(HOST, ["clarity"])
    result = data["sources"]["clarity"]
    assert (result["calls_used_today"], result["calls_left"]) == (1, 9)


async def test_a_cached_answer_shows_the_count_and_spends_none(live):
    await run("clarity", INSIGHTS)
    data = await run("clarity", INSIGHTS)
    assert data["from_cache"] is True
    assert (data["calls_used_today"], data["calls_left"]) == (1, 9)


async def test_a_call_that_fails_still_shows_what_it_spent(live, fake_net):
    fake_net(lambda _: httpx2.Response(500))
    data = await run("clarity", INSIGHTS)
    assert data["status"] == "error"
    assert (data["calls_used_today"], data["calls_left"]) == (1, 9)


async def test_a_timeout_still_shows_what_it_spent(live, fake_net, monkeypatch):
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(net.httpx2.AsyncClient, "request", slow)
    data = await run("clarity", INSIGHTS)
    assert "timed out" in data["reason"]
    assert data["calls_used_today"] == 1


async def test_a_source_that_spends_nothing_shows_no_count(live):
    data = await core.get_seo_data(HOST, ["sitemaps"])
    assert "calls_used_today" not in data["sources"]["sitemaps"]


# The server stops before the limit.
async def test_a_refused_call_names_the_reset_and_sends_nothing(live):
    set_ledger(clarity.LEDGER, HOST, used=10)
    data = await run("clarity", DASHBOARD)
    assert data["status"] == "error"
    assert data["calls_left"] == 0
    assert data["resets_at"] in data["reason"]
    assert "assumption" in data["reason"]
    assert not live


@pytest.mark.parametrize(("used", "warned"), [(7, False), (8, True), (9, True)])
async def test_a_warning_appears_when_few_calls_are_left(live, used, warned):
    set_ledger(clarity.LEDGER, HOST, used=used - 1)
    data = await run("clarity", INSIGHTS)
    assert data["calls_used_today"] == used
    assert ("quota_warning" in data) is warned
    if warned:
        assert str(10 - used) in data["quota_warning"]


@pytest.mark.parametrize("value", [-1, 1.5, True, "3", None])
async def test_a_ledger_with_a_bad_count_fails_closed(live, value):
    set_ledger(clarity.LEDGER, HOST, used=value)
    data = await run("clarity", INSIGHTS)
    assert data["status"] == "error"
    assert "ledger" in data["reason"]
    assert data["calls_used_today"] is None
    assert data["calls_left"] is None
    assert not live


async def test_a_429_of_the_export_api_blocks_the_day_with_no_false_count(live, fake_net):
    calls = fake_net(lambda _: httpx2.Response(429))
    first = await run("clarity", INSIGHTS)
    assert first["blocked_by_api"] is True
    assert (first["calls_used_today"], first["calls_left"]) == (1, 0)
    second = await run("clarity", DASHBOARD)
    assert "HTTP 429" in second["reason"]
    assert second["resets_at"] in second["reason"]
    assert len(calls) == 1


async def test_a_429_of_an_mcp_endpoint_does_not_block_the_day(live, fake_net):
    calls = fake_net(lambda _: httpx2.Response(429))
    first = await run("clarity", DASHBOARD)
    assert "blocked_by_api" not in first
    assert first["calls_left"] == 9
    await run("clarity", RECORDINGS)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("api", "call", "ledger", "key"),
    [
        ("clarity", DASHBOARD, clarity.LEDGER, HOST),
        ("search_console", INSPECT, gsc.LEDGER, SITE["gsc_property"]),
    ],
)
@pytest.mark.parametrize("day", ["not a date", "2026-1-31", "20260131", "2099-01-01", 5, None])
async def test_a_ledger_with_a_wrong_day_fails_closed(live, day, api, call, ledger, key):
    set_ledger(ledger, key, used=ledger.limit, blocked=True)
    path = quota.path(ledger, key)
    path.write_text(json.dumps({"day": day, "used": ledger.limit, "blocked": True}))
    before = path.read_text()
    data = await run(api, call)
    assert data["status"] == "error"
    assert "ledger" in data["reason"]
    assert data["calls_left"] is None
    assert not metered_requests(live)
    assert path.read_text() == before


async def test_a_late_429_leaves_the_ledger_of_the_next_day(live):
    set_ledger(clarity.LEDGER, HOST, used=2)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    await quota.block(clarity.LEDGER, HOST, net.Receipt(clarity.LEDGER.name, 0, yesterday))
    assert quota.view(clarity.LEDGER, HOST)["calls_used_today"] == 2
    assert "blocked_by_api" not in quota.view(clarity.LEDGER, HOST)


async def test_a_block_of_an_older_day_ends(live):
    path = quota.path(clarity.LEDGER, HOST)
    path.parent.mkdir(parents=True)
    old = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    path.write_text(json.dumps({"day": old, "used": 3, "blocked": True}))
    assert (await run("clarity", INSIGHTS))["status"] == "ok"


async def test_a_connection_that_fails_before_the_send_still_counts(live):
    def refuse(request):
        raise httpx2.ConnectError("no route")

    net.transport = httpx2.MockTransport(refuse)
    data = await run("clarity", DASHBOARD)
    assert data["status"] == "error"
    assert data["calls_used_today"] == 1


# URL Inspection: 2000 for each site each day.
async def test_url_inspection_spends_from_the_ledger_of_the_property(live):
    data = await run("search_console", INSPECT)
    assert data["status"] == "ok"
    assert (data["calls_used_today"], data["daily_limit"], data["calls_left"]) == (1, 2000, 1999)
    assert quota.path(gsc.LEDGER, SITE["gsc_property"]).exists()


async def test_index_status_spends_1_call_for_each_url(live):
    data = await core.get_seo_data(HOST, ["index_status"], page="/a")
    assert data["sources"]["index_status"]["calls_used_today"] == 1


async def test_index_status_spends_all_its_urls_or_none(live, fake_net):
    calls = fake_net(
        lambda request: (
            httpx2.Response(
                200,
                json={
                    "rows": [
                        {
                            "keys": [f"https://{HOST}/{n}"],
                            "clicks": 1,
                            "impressions": 1,
                            "ctr": 1,
                            "position": 1,
                        }
                        for n in range(9)
                    ]
                },
            )
            if "searchAnalytics" in request.url.path
            else answer(request)
        )
    )
    set_ledger(gsc.LEDGER, SITE["gsc_property"], used=1998)
    data = await core.get_seo_data(HOST, ["index_status"])
    result = data["sources"]["index_status"]
    assert result["status"] == "error"
    assert result["calls_used_today"] == 1998
    assert not [call for call in sent(calls) if "urlInspection" in call.url.path]


# The guard: a metered endpoint opens only with a receipt of its ledger.
METERED = {
    ("www.clarity.ms", r"/export-data/api/v1/project-live-insights"),
    ("clarity.microsoft.com", r"/mcp/recordings/sample"),
    ("clarity.microsoft.com", r"/mcp/dashboard/query"),
    ("searchconsole.googleapis.com", r"/v1/urlInspection/index:inspect"),
}
ENDPOINT = net.Endpoint("GET", "api.example.test", r"/v1/read", "a-ledger")
URL = "https://api.example.test/v1/read"


def test_each_endpoint_with_a_daily_call_limit_is_metered():
    found = {(e.host, e.path) for e in providers.ENDPOINTS if e.metered}
    assert found == METERED, (
        f"Fix: name the ledger in metered in ENDPOINTS, or update METERED. {found ^ METERED}"
    )


async def test_a_metered_endpoint_needs_a_receipt(fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json={}))
    with pytest.raises(net.NotAllowed):
        await net.request((ENDPOINT,), "GET", URL)
    assert not calls


async def test_a_receipt_of_another_ledger_opens_nothing(fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json={}))
    with pytest.raises(net.NotAllowed):
        await net.request((ENDPOINT,), "GET", URL, spent=net.Receipt("other", 1, TODAY))
    assert not calls


async def test_a_receipt_opens_only_the_calls_that_it_paid_for(fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json={}))
    receipt = net.Receipt("a-ledger", 1, TODAY)
    await net.request((ENDPOINT,), "GET", URL, spent=receipt)
    with pytest.raises(net.NotAllowed):
        await net.request((ENDPOINT,), "GET", URL, spent=receipt)
    assert len(calls) == 1


# GA4 gives the real value, so the server prefers it over a local count.
def ga4_reply(request):
    if request.url.host == ga4.HOST:
        return httpx2.Response(200, json={"rows": [], "propertyQuota": GA4_QUOTA})
    return None


@pytest.mark.parametrize("method", ["run_report", "pivot_report", "realtime_report"])
async def test_each_ga4_report_asks_for_the_quota(live, fake_net, method):
    calls = fake_net(replies(ga4_reply))
    await core.query_api(HOST, "ga4", method, {})
    assert json.loads(sent(calls)[0].content)["returnPropertyQuota"] is True


async def test_a_ga4_source_asks_for_the_quota_and_shows_it(live, fake_net):
    calls = fake_net(replies(ga4_reply))
    data = await core.get_seo_data(HOST, ["ga4_organic"])
    result = data["sources"]["ga4_organic"]
    assert json.loads(sent(calls)[0].content)["returnPropertyQuota"] is True
    assert result["tokens_left"] == 199_000
    assert result["tokens_spent"] == 12


async def test_a_ga4_answer_shows_the_value_of_the_api(live, fake_net):
    fake_net(replies(ga4_reply))
    data = await core.query_api(HOST, "ga4", "run_report", {})
    assert data["tokens_left"] == 199_000
    assert data["tokens_left_this_hour"] == 39_000
    assert data["tokens_left_for_project_this_hour"] == 13_000
    assert data["tokens_spent"] == 12
    assert data["tokens_used_today"] == 12
    assert data["daily_token_limit"] == {"standard": 200_000, "analytics_360": 2_000_000}
    assert data["reset_stated"] is True


async def test_the_server_does_not_infer_the_ga4_tier(live, fake_net):
    low = {"tokensPerDay": {"consumed": 1, "remaining": 150_000}}

    def reply(request):
        if request.url.host == ga4.HOST:
            return httpx2.Response(200, json={"propertyQuota": low})
        return None

    fake_net(replies(reply))
    data = await core.query_api(HOST, "ga4", "run_report", {})
    assert data["tokens_used_today"] == 1


async def test_ga4_tokens_add_up_over_the_day(live, fake_net):
    fake_net(replies(ga4_reply))
    await core.query_api(HOST, "ga4", "run_report", {})
    data = await core.query_api(HOST, "ga4", "run_report", {"limit": 5})
    assert data["tokens_used_today"] == 24


async def test_a_caller_can_turn_the_ga4_quota_off(live, fake_net):
    calls = fake_net(replies(lambda _: None))
    await core.query_api(HOST, "ga4", "run_report", {"returnPropertyQuota": False})
    assert json.loads(sent(calls)[0].content)["returnPropertyQuota"] is False


async def test_realtime_tokens_are_a_separate_pool(live, fake_net):
    fake_net(replies(ga4_reply))
    await core.query_api(HOST, "ga4", "realtime_report", {})
    view = ga4.now(core.config.resolve(HOST))
    assert view["realtime"]["tokens_left"] == 199_000
    assert view["core"]["tokens_left"] is None


async def test_a_ga4_reading_of_an_older_day_is_not_shown_as_today(live, fake_net):
    fake_net(replies(ga4_reply))
    await core.query_api(HOST, "ga4", "run_report", {})
    path = ga4.reading_path(SITE["ga4_property_id"], "core")
    stored = json.loads(path.read_text())
    stored["day"] = "2020-01-01"
    path.write_text(json.dumps(stored))
    view = ga4.now(core.config.resolve(HOST))["core"]
    assert view["tokens_left"] is None
    assert view["tokens_used_today"] == 0
    assert "no reading" in view["note"]


async def test_few_ga4_tokens_give_a_warning(live, fake_net):
    low = {"tokensPerDay": {"consumed": 1, "remaining": 10}}

    def reply(request):
        if request.url.host == ga4.HOST:
            return httpx2.Response(200, json={"propertyQuota": low})
        return None

    fake_net(replies(reply))
    data = await core.query_api(HOST, "ga4", "run_report", {})
    assert "10" in data["quota_warning"]


# An agent sees all of it with no network call.
async def test_list_sites_shows_the_quota_with_no_network(live):
    await run("clarity", INSIGHTS)
    live.clear()
    (row,) = core.list_sites()["rows"]
    calls = row["quota"]["clarity"]["calls"]
    assert (calls["calls_used_today"], calls["daily_limit"], calls["calls_left"]) == (1, 10, 9)
    assert row["quota"]["search_console"]["url_inspection"]["calls_left"] == 2000
    assert row["quota"]["ga4"]["core"]["tokens_left"] is None
    assert row["quota"]["crux"]["tracked"] is False
    assert "150" in row["quota"]["crux"]["limits"][0]
    assert "no number" in row["quota"]["bing"]["limits"][0]
    assert not live


async def test_list_sites_shows_only_connected_apis(write_config):
    write_config()
    (row,) = core.list_sites()["rows"]
    assert set(row["quota"]) == {"pagespeed"}


async def test_list_sites_survives_a_broken_ledger(live):
    path = quota.path(clarity.LEDGER, HOST)
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    (row,) = core.list_sites()["rows"]
    calls = row["quota"]["clarity"]["calls"]
    assert calls["calls_left"] is None
    assert "ledger" in calls["quota_warning"]


async def test_discovery_shows_limits_and_the_count_of_the_site(live):
    set_ledger(clarity.LEDGER, HOST, used=4)
    data = await core.query_api(HOST, "clarity")
    assert data["quota"]["docs"].startswith("https://learn.microsoft.com/")
    assert data["quota"]["now"]["calls"]["calls_left"] == 6
    listed = await core.query_api(HOST)
    row = next(row for row in listed["rows"] if row["api"] == "clarity")
    assert row["quota"]["now"]["calls"]["calls_left"] == 6
    found = await core.query_api(HOST, task="dead clicks")
    assert found["rows"][0]["quota"]["now"]["calls"]["calls_left"] == 6
    assert not live


async def test_discovery_with_no_sites_file_shows_the_published_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_MCP_CONFIG", str(tmp_path / "missing.json"))
    data = await core.query_api(api="clarity")
    assert data["quota"]["limits"]
    assert "now" not in data["quota"]


@pytest.mark.parametrize("name", sorted(providers.APIS))
def test_each_api_states_its_limit_with_a_doc_link(name):
    entry = providers.APIS[name].quota
    assert entry.docs.startswith("https://")
    assert entry.limits
    assert any(char.isdigit() for text in entry.limits for char in text) or "no number" in str(
        entry.limits
    )


def test_the_readme_links_the_quota_docs_of_each_api():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    for name, entry in providers.APIS.items():
        assert entry.quota.docs in readme, name


def metered_requests(calls):
    return [
        call
        for call in calls
        for endpoint in providers.ENDPOINTS
        if endpoint.metered
        and endpoint.host == call.url.host
        and re.fullmatch(endpoint.path, call.url.path)
    ]


def spent_calls():
    keys = ((clarity.LEDGER, HOST), (gsc.LEDGER, SITE["gsc_property"]))
    return sum(quota.view(ledger, key)["calls_used_today"] for ledger, key in keys)


# The fact that the whole design rests on: the count equals the calls that went out.
@pytest.mark.parametrize("source", sorted(providers.SOURCES))
async def test_each_source_counts_each_metered_call(live, source):
    await core.get_seo_data(HOST, [source])
    assert len(metered_requests(live)) == spent_calls()


@pytest.mark.parametrize(
    ("api", "call"),
    [
        ("clarity", INSIGHTS),
        ("clarity", RECORDINGS),
        ("clarity", DASHBOARD),
        ("search_console", INSPECT),
    ],
)
async def test_each_metered_method_counts_each_call(live, api, call):
    await run(api, call)
    assert len(metered_requests(live)) == spent_calls() == 1


async def test_a_bad_ga4_reading_does_not_lose_a_paid_answer(live, fake_net):
    fake_net(replies(ga4_reply))
    path = ga4.reading_path(SITE["ga4_property_id"], "core")
    await core.query_api(HOST, "ga4", "run_report", {})
    stored = json.loads(path.read_text())
    path.write_text(json.dumps({**stored, "tokens_used_today": "10"}))
    data = await core.query_api(HOST, "ga4", "run_report", {})
    assert data["status"] == "ok"


def test_the_server_needs_no_timezone_data():
    for file in (Path(__file__).parent.parent / "src").rglob("*.py"):
        assert "zoneinfo" not in file.read_text(), (
            f"{file.name}: a host with no timezone data cannot start the server. "
            "Use a fixed offset from the docs."
        )
