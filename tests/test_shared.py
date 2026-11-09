"""Identical requests share 1 live call, and a saved answer shows its age and its end."""

import ast
import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from seo_mcp import config, core, quota, shared
from seo_mcp.providers import clarity, gsc, pagespeed
from test_query_api import HOST, SITE
from test_quota import (
    DASHBOARD,
    INSPECT,
    ga4_reply,
    metered_requests,
    replies,
    run,
    sent,
    set_ledger,
)

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
RANGE = {"startDate": "2026-01-01", "endDate": "2026-01-10"}
GA4_REPORT = ("run_report", {"metrics": [{"name": "sessions"}]})
REALTIME = ("realtime_report", {"metrics": [{"name": "activeUsers"}]})


def query(api, method, request=None):
    return core.query_api(HOST, api, method, request or {})


def sources(*names, **params):
    return core.get_seo_data(HOST, list(names), **params)


def age_all(seconds):
    """Make each saved answer older, as time would."""
    for path in shared.folder().glob("*.json"):
        stored = json.loads(path.read_text())
        field = "failed_at" if "failed_at" in stored else "cached_at"
        at = datetime.fromisoformat(stored[field]) - timedelta(seconds=seconds)
        path.write_text(json.dumps({**stored, field: at.isoformat()}))


REPEATS = [
    ("page_speed", {}),
    ("page_speed", {"page": "/a"}),
    ("chrome_field_data", {}),
    ("search_queries", {}),
    ("search_pages", {}),
    ("quick_wins", {}),
    ("sitemaps", {}),
    ("ga4_organic", {}),
    ("ga4_landing_pages", {}),
    ("index_status", {"page": "/a"}),
]
METHODS = [
    ("pagespeed", "run", {"url": "/a"}),
    ("crux", "record", {}),
    ("crux", "history", {}),
    ("search_console", "search_analytics", RANGE),
    ("search_console", "url_inspection", INSPECT[1]),
    ("search_console", "sitemaps", {}),
    ("search_console", "sitemap", {"feedpath": f"https://{HOST}/s.xml"}),
    ("ga4", *GA4_REPORT),
    ("ga4", "metadata", {}),
    ("bing", "GetQueryStats", {}),
]


@pytest.mark.parametrize(("name", "params"), REPEATS)
async def test_a_repeat_of_a_source_sends_nothing(live, name, params):
    first = (await sources(name, **params))["sources"][name]
    count = len(sent(live))
    assert count
    assert first["status"] == "ok"
    assert first["from_cache"] is False
    second = (await sources(name, **params))["sources"][name]
    assert len(sent(live)) == count
    assert second["status"] == "ok"
    assert second["rows"] == first["rows"]
    assert second["from_cache"] is True
    assert second["cached_at"] == first["cached_at"]
    assert second["cache_age_seconds"] >= 0
    assert second["cache_expires_at"] > second["cached_at"]


@pytest.mark.parametrize(("api", "method", "request_body"), METHODS)
async def test_a_repeat_of_a_method_sends_nothing(live, api, method, request_body):
    first = await query(api, method, request_body)
    count = len(sent(live))
    assert first["status"] == "ok"
    assert first["from_cache"] is False
    second = await query(api, method, request_body)
    assert len(sent(live)) == count
    assert second["from_cache"] is True
    assert second["rows"] == first["rows"]


async def test_a_repeat_of_bing_links_sends_nothing(live, fake_net):
    def reply(request):
        if request.url.host == "ssl.bing.com":
            links = {"Links": [{"Url": "u", "Count": 12}]}
            return httpx2.Response(200, json={"d": links})
        return None

    calls = fake_net(replies(reply))
    await sources("bing_links")
    data = (await sources("bing_links"))["sources"]["bing_links"]
    assert data["from_cache"] is True
    assert data["totals"] == {"inbound_links": 12}
    assert len(sent(calls)) == 1


async def test_parallel_identical_calls_share_1_request(live):
    results = await asyncio.gather(*(sources("page_speed") for _ in range(18)))
    assert len(sent(live)) == 1
    flags = [result["sources"]["page_speed"]["from_cache"] for result in results]
    assert flags.count(False) == 1


async def test_2_sources_share_their_identical_request(live):
    await sources("search_pages")
    count = len(sent(live))
    data = await sources("index_status", page=f"https://{HOST}/a")
    await sources("quick_wins")
    assert data["sources"]["index_status"]["status"] == "ok"
    assert len(sent(live)) <= count + 2


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("pagespeed", "run", {"url": "/a"}), ("pagespeed", "run", {"url": "/b"})),
        (("pagespeed", "run", {"url": "/a"}), ("pagespeed", "run", {"url": "/a", "locale": "de"})),
        (("crux", "record", {}), ("crux", "history", {})),
        (
            ("search_console", "search_analytics", RANGE),
            ("search_console", "search_analytics", {**RANGE, "dimensions": ["page"]}),
        ),
    ],
)
async def test_different_requests_do_not_share(live, first, second):
    await query(*first)
    count = len(sent(live))
    data = await query(*second)
    assert data["from_cache"] is False
    assert len(sent(live)) > count


async def test_2_sites_do_not_share(live, tmp_path, write_config, account):
    await sources("page_speed", page=f"https://{HOST}/a")
    count = len(sent(live))
    write_config({**SITE, "host": "other.example.com"}, account)
    data = await core.get_seo_data("other.example.com", ["page_speed"], page="/a")
    assert data["sources"]["page_speed"]["from_cache"] is False
    assert len(sent(live)) == count + 1


async def test_a_saved_answer_ends_at_the_stated_time(live):
    first = (await sources("page_speed"))["sources"]["page_speed"]
    said = datetime.fromisoformat(first["cache_expires_at"])
    assert said - datetime.fromisoformat(first["cached_at"]) == timedelta(
        seconds=shared.ANSWER_SECONDS
    )
    age_all(shared.ANSWER_SECONDS)
    second = (await sources("page_speed"))["sources"]["page_speed"]
    assert second["from_cache"] is False
    assert len(sent(live)) == 2


async def test_a_saved_answer_of_the_future_is_not_served(live):
    await sources("page_speed")
    age_all(-shared.ANSWER_SECONDS * 2)
    assert (await sources("page_speed"))["sources"]["page_speed"]["from_cache"] is False


async def test_a_realtime_report_stays_only_a_short_time(live, fake_net):
    first = await query("ga4", *REALTIME)
    said = datetime.fromisoformat(first["cache_expires_at"])
    span = said - datetime.fromisoformat(first["cached_at"])
    assert span == timedelta(seconds=shared.REALTIME_SECONDS)


async def test_a_repeat_of_url_inspection_spends_nothing(live):
    await query("search_console", *("url_inspection", INSPECT[1]))
    data = await query("search_console", *("url_inspection", INSPECT[1]))
    assert data["calls_used_today"] == 1
    assert data["from_cache"] is True
    assert len(metered_requests(live)) == 1


async def test_a_saved_inspection_serves_at_the_limit(live):
    await query("search_console", *("url_inspection", INSPECT[1]))
    set_ledger(gsc.LEDGER, SITE["gsc_property"], used=gsc.LEDGER.limit)
    data = await query("search_console", *("url_inspection", INSPECT[1]))
    assert data["status"] == "ok"
    assert data["from_cache"] is True


async def test_a_repeat_of_index_status_spends_nothing(live):
    first = (await sources("index_status"))["sources"]["index_status"]
    assert first["status"] == "ok"
    second = (await sources("index_status"))["sources"]["index_status"]
    assert second["calls_used_today"] == first["calls_used_today"]
    assert second["rows"] == first["rows"]
    assert second["from_cache"] is True


async def test_2_properties_of_1_host_do_not_share_index_status(live, write_config, account):
    await sources("index_status", page=f"https://{HOST}/a")
    write_config({**SITE, "gsc_property": "https://www.example.com/"}, account)
    data = (await sources("index_status", page=f"https://{HOST}/a"))["sources"]["index_status"]
    assert data["from_cache"] is False


async def test_a_batch_with_1_sent_failure_keeps_the_window(live, fake_net):
    def reply(request):
        if request.url.path.endswith("index:inspect"):
            if json.loads(request.content)["inspectionUrl"] == f"https://{HOST}/":
                raise httpx2.ConnectError("no route")
            return httpx2.Response(500)
        return None

    fake_net(replies(reply))
    first = (await sources("index_status"))["sources"]["index_status"]
    second = (await sources("index_status"))["sources"]["index_status"]
    assert first["status"] == "error"
    assert "did not send it again" in second["reason"]
    assert second["calls_used_today"] == first["calls_used_today"]


async def test_a_full_disk_does_not_lose_a_paid_answer(tmp_path):
    target = tmp_path / "entry.json"
    target.mkdir()

    async def send(_):
        return {"paid": True}

    assert await shared.once(target, send) == {"paid": True}


async def test_a_prune_reaches_entries_behind_many_lock_files(live, monkeypatch):
    monkeypatch.setattr(shared, "PRUNE_BATCH", 1)
    shared.folder().mkdir(parents=True)
    for number in range(200):
        (shared.folder() / f"{number}.lock").touch()
    stale = shared.folder() / "stale.json"
    stale.write_text("{}")
    long_ago = datetime.now(UTC).timestamp() - shared.PRUNE_SECONDS - 1
    os.utime(stale, (long_ago, long_ago))
    await sources("page_speed")
    assert not stale.exists()


async def test_a_longer_lifetime_does_not_extend_a_short_saved_answer(live):
    site = config.resolve(HOST)
    args = (site, pagespeed.ENDPOINTS, "GET", pagespeed.PSI_API)
    await shared.request(*args, keep=30)
    age_all(31)
    await shared.request(*args, keep=300)
    assert len(sent(live)) == 2


async def test_a_repeat_of_a_ga4_report_does_not_count_its_tokens_twice(live, fake_net):
    def reply(request):
        if request.url.path.endswith(":runReport"):
            usage = {"tokensPerDay": {"consumed": 12, "remaining": 100}}
            body = {"rows": [], "propertyQuota": usage, "kind": "analyticsData#runReport"}
            return httpx2.Response(200, json=body)
        return None

    fake_net(replies(reply))
    await query("ga4", *GA4_REPORT)
    data = await query("ga4", *GA4_REPORT)
    assert data["tokens_used_today"] == 12


@pytest.mark.parametrize(
    ("name", "host"),
    [
        ("page_speed", "www.googleapis.com"),
        ("bing_links", "ssl.bing.com"),
        ("chrome_field_data", "chromeuxreport.googleapis.com"),
    ],
)
async def test_a_failure_of_any_source_stops_a_repeat(live, fake_net, name, host):
    def reply(request):
        return httpx2.Response(500) if request.url.host == host else None

    calls = fake_net(replies(reply))
    await sources(name)
    data = (await sources(name))["sources"][name]
    assert "did not send it again" in data["reason"]
    assert data["status"] == "error"
    assert len(sent(calls)) == 1


async def test_a_failure_goes_out_again_after_the_window(live, fake_net):
    def reply(request):
        return httpx2.Response(500) if "pagespeed" in request.url.path else None

    calls = fake_net(replies(reply))
    await sources("page_speed")
    age_all(shared.FAILURE_WINDOW_SECONDS)
    await sources("page_speed")
    assert len(sent(calls)) == 2


CACHE_FIELDS = {"from_cache", "cached_at", "cache_age_seconds", "cache_expires_at"}


async def test_a_404_of_crux_shows_the_cache_fields(live, fake_net):
    """A live test: a chrome_field_data answer had no cache fields, and the text says it has."""

    def reply(request):
        return httpx2.Response(404) if "records:" in request.url.path else None

    fake_net(replies(reply))
    first = (await sources("chrome_field_data"))["sources"]["chrome_field_data"]
    again = (await sources("chrome_field_data"))["sources"]["chrome_field_data"]
    fix = "A live answer from a failed call shows the cache fields (shared.once)."
    assert CACHE_FIELDS <= set(first), fix
    assert first["from_cache"] is False
    assert again["from_cache"] is True


@pytest.mark.parametrize(("name", "params"), REPEATS)
async def test_each_network_source_shows_the_cache_fields(live, name, params):
    result = (await sources(name, **params))["sources"][name]
    assert result["status"] == "ok"
    missing = sorted(CACHE_FIELDS - set(result))
    assert not missing, f"{name} misses {missing}: note each call in shared.once."


async def test_a_saved_ga4_answer_spent_no_token(live, fake_net):
    """A live test: a saved GA4 answer showed tokens_spent 1 from the reading of the old call."""
    fake_net(replies(ga4_reply))
    live_answer = (await sources("ga4_organic"))["sources"]["ga4_organic"]
    saved = (await sources("ga4_organic"))["sources"]["ga4_organic"]
    assert live_answer["tokens_spent"] == 12
    assert saved["from_cache"] is True
    assert saved["tokens_spent"] == 0
    # The reading of the day stays: only the spend of this answer is 0.
    assert saved["tokens_used_today"] == 12


async def test_a_404_of_crux_stays_the_no_data_answer_on_a_repeat(live, fake_net):
    def reply(request):
        return httpx2.Response(404) if "records:" in request.url.path else None

    calls = fake_net(replies(reply))
    await sources("chrome_field_data")
    data = (await sources("chrome_field_data"))["sources"]["chrome_field_data"]
    assert data["status"] == "ok"
    assert "no field data" in data["reason"]
    assert len(sent(calls)) == 1


@pytest.mark.parametrize(
    ("name", "host", "body"),
    [
        ("page_speed", "www.googleapis.com", {"message": "x"}),
        ("chrome_field_data", "chromeuxreport.googleapis.com", {"message": "x"}),
        ("bing_links", "ssl.bing.com", {"message": "x"}),
        ("sitemaps", "searchconsole.googleapis.com", []),
    ],
)
async def test_a_bad_answer_of_any_source_is_not_saved(live, fake_net, name, host, body):
    def reply(request):
        return httpx2.Response(200, json=body) if request.url.host == host else None

    fake_net(replies(reply))
    data = (await sources(name))["sources"][name]
    assert data["status"] == "error"
    assert "shape" in data["reason"]
    assert not [path for path in shared.folder().glob("*.json") if "failed" not in path.name]


async def test_a_saved_file_holds_no_secret(live, fake_net):
    def reply(request):
        if request.url.host == "ssl.bing.com":
            return httpx2.Response(200, json={"d": {"Links": [{"Url": "bing-key-value-5678"}]}})
        return None

    fake_net(replies(reply))
    await sources("bing_links")
    text = "".join(path.read_text() for path in shared.folder().glob("*"))
    assert "bing-key-value-5678" not in text
    names = "".join(path.name for path in shared.folder().glob("*"))
    assert "bing-key-value-5678" not in names


async def test_a_failure_file_holds_no_secret(live, fake_net):
    def reply(request):
        if "runPagespeed" in request.url.path:
            message = {"error": {"message": "bad key=google-key-value-1234"}}
            return httpx2.Response(400, json=message)
        return None

    fake_net(replies(reply))
    await query("pagespeed", "run", {"url": "/a"})
    text = "".join(path.read_text() for path in shared.folder().glob("*.json"))
    assert "google-key-value-1234" not in text


async def test_a_saved_request_still_needs_a_listed_endpoint(live, monkeypatch):
    await sources("page_speed")
    monkeypatch.setattr("seo_mcp.providers.pagespeed.ENDPOINTS", ())
    data = (await sources("page_speed"))["sources"]["page_speed"]
    assert data["status"] == "error"


async def test_a_source_of_2_requests_is_from_cache_only_when_both_are(live):
    await sources("search_pages")
    count = len(sent(live))
    data = (await sources("index_status"))["sources"]["index_status"]
    assert data["from_cache"] is False
    assert len(sent(live)) > count
    assert data["cache_age_seconds"] >= 0


async def test_a_source_with_no_network_shows_no_cache_fields(live):
    data = await core.query_api(HOST)
    assert "from_cache" not in data


async def test_old_saved_entries_are_pruned_and_ledgers_stay(live):
    set_ledger(clarity.LEDGER, HOST, used=3)
    stale = shared.folder() / "stale.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}")
    long_ago = datetime.now(UTC).timestamp() - shared.PRUNE_SECONDS - 1
    os.utime(stale, (long_ago, long_ago))
    await sources("page_speed")
    assert not stale.exists()
    assert quota.view(clarity.LEDGER, HOST)["calls_used_today"] == 3


async def test_a_broken_saved_file_is_a_miss(live):
    await sources("page_speed")
    for path in shared.folder().glob("*.json"):
        path.write_text("not json")
    data = (await sources("page_speed"))["sources"]["page_speed"]
    assert data["status"] == "ok"
    assert data["from_cache"] is False


# The Clarity day cache keeps its own end, through the same guard.
async def test_a_clarity_answer_states_the_end_of_the_utc_day(live):
    await run("clarity", DASHBOARD)
    data = await run("clarity", DASHBOARD)
    assert data["from_cache"] is True
    assert data["cache_expires_at"].endswith("T00:00:00+00:00")
    assert data["cache_age_seconds"] >= 0


@pytest.mark.parametrize("text", [" q", "q ", "  q\n", "\tq"])
async def test_outer_spaces_do_not_make_a_new_clarity_call(live, text):
    await run("clarity", DASHBOARD)
    data = await run("clarity", ("dashboard_query", {"query": text}))
    assert data["from_cache"] is True
    assert data["applied"]["query"] == "q"
    assert len(metered_requests(live)) == 1


@pytest.mark.parametrize("other", ["Q", "q  q", "q q", "q?"])
async def test_2_different_questions_stay_2_calls(live, other):
    await run("clarity", ("dashboard_query", {"query": "q q"}))
    data = await run("clarity", ("dashboard_query", {"query": other + " x"}))
    assert data["from_cache"] is False


async def test_only_outer_spaces_are_trimmed(live):
    await run("clarity", ("dashboard_query", {"query": "a b"}))
    data = await run("clarity", ("dashboard_query", {"query": "a  b"}))
    assert data["from_cache"] is False
    assert len(metered_requests(live)) == 2


def test_18_real_processes_send_1_request(tmp_path, write_config, repo):
    write_config({"secrets": {}})
    cache = tmp_path / "shared-cache"
    child = textwrap.dedent(
        f"""
        import asyncio, httpx2
        from seo_mcp import core, net
        sent = []
        class Edge(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request):
                sent.append(1)
                await asyncio.sleep(0.5)
                reply = {{"lighthouseResult": {{"categories": {{}}, "audits": {{}}}}}}
                return httpx2.Response(200, json=reply)
        net.transport = Edge()
        data = asyncio.run(core.get_seo_data("{HOST}", ["page_speed"]))
        print(len(sent), data["sources"]["page_speed"]["status"])
        """
    )
    env = {**os.environ, "SEO_MCP_CACHE_DIR": str(cache)}
    runs = [
        subprocess.Popen([sys.executable, "-c", child], env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(18)
    ]
    outputs = [process.communicate(timeout=60)[0].split() for process in runs]
    assert sum(int(output[0]) for output in outputs) == 1
    assert {output[1] for output in outputs} == {"ok"}


ALLOWED = {
    ("_google.py", "token"),
    ("clarity.py", "call"),
    ("ga4.py", "send"),
    ("gsc.py", "_inspect"),
    ("gsc.py", "send"),
    ("shared.py", "send"),
}


def _callers(path, called):
    def visit(node, name):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = node.name
        if isinstance(node, ast.Call) and ast.unparse(node.func) == called:
            yield path.name, name
        for child in ast.iter_child_nodes(node):
            yield from visit(child, name)

    yield from visit(ast.parse(path.read_text(encoding="utf-8")), "module")


def test_each_api_call_goes_through_the_shared_guard():
    found = {
        pair for path in sorted(SOURCE.rglob("*.py")) for pair in _callers(path, "net.request")
    }
    wrong = sorted(found - ALLOWED)
    assert not wrong, f"Call shared.request, so that repeats send nothing: {wrong}"


def test_each_ledger_spend_is_in_a_prepare_step():
    found = {
        pair for path in sorted(SOURCE.rglob("*.py")) for pair in _callers(path, "quota.spend")
    }
    wrong = sorted(pair for pair in found if pair[1] != "prepare")
    assert found
    assert not wrong, f"Spend in the prepare step of shared.once, for the failure window: {wrong}"
