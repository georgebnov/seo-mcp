"""get_seo_data runs each real source at the same time. Only the transport is fake.

The transport answers each API host by a rule: an answer, an empty answer, an HTTP status, an
exception, or a wait. Bing has no bing_site_url, so it is not connected.
"""

import asyncio
import json

import httpx2
import pytest

from seo_mcp import core, net, providers
from seo_mcp.providers import ga4, gsc
from test_query_api import SITE, answer

CRUX = "chromeuxreport.googleapis.com"
CLARITY = "www.clarity.ms"
BING = "ssl.bing.com"
LEAKY_PAGE = "https://x.test/?apikey=abc123"
SECRET_TEXT = "secret-text-from-an-exception"
# The status and the reason of each provider in the default rules.
EXPECTED = {
    "pagespeed": ("ok", None),
    "crux": ("ok", "Chrome has no field data for this site."),
    "bing": ("not_connected", None),
    "clarity": ("error", f"HTTP 403 from the API. {core.STATUS_HINTS[403]}"),
    "ga4": ("error", "The source failed with RuntimeError."),
    "search_console": ("error", "The source timed out after 0.05 seconds."),
}


class Edge(httpx2.AsyncBaseTransport):
    """Answer each request by the rule of its host. A host with no rule gets the test answer."""

    def __init__(self, rules):
        self.rules = rules
        self.cancelled = []

    async def handle_async_request(self, request):
        rule = self.rules.get(request.url.host)
        if rule == "wait":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled.append(request.url.host)
                raise
        if rule == "raise":
            raise RuntimeError(SECRET_TEXT)
        if isinstance(rule, int):
            return httpx2.Response(rule)
        return rule(request) if callable(rule) else answer(request)


@pytest.fixture
def edge(write_config, account, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-1234")
    site = {name: value for name, value in SITE.items() if name != "bing_site_url"}
    write_config(site, {**account, "google_api_key": {"env": "GOOGLE_KEY_ENV"}})
    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 0.05)
    transport = Edge({CRUX: 404, CLARITY: 403, ga4.HOST: "raise", gsc.HOST: "wait", BING: 500})
    net.transport = transport
    return transport


async def test_only_a_site_runs_every_source(edge):
    data = await core.get_seo_data("www.example.com")
    assert set(data["sources"]) == set(providers.SOURCES)
    assert data["status"] == "ok"
    for name, result in data["sources"].items():
        expected, reason = EXPECTED[providers.PROVIDER_OF[name]]
        assert result["status"] == expected, name
        if reason:
            assert result["reason"] == reason, name
    assert {"kind": "score", "metric": "seo", "value": 100} in data["sources"]["page_speed"]["rows"]
    assert data["thin_data"] is True
    assert sorted(data["thin_sources"]) == sorted(set(providers.SOURCES) - {"page_speed"})
    states = [EXPECTED[providers.PROVIDER_OF[name]][0] for name in providers.SOURCES]
    assert data["counts"] == {
        state: states.count(state) for state in ("ok", "not_connected", "error")
    }
    assert data["counts"]["ok"] == 2


async def test_sources_limits_the_call(edge):
    data = await core.get_seo_data("www.example.com", ["page_speed"])
    assert list(data["sources"]) == ["page_speed"]
    assert data["thin_data"] is False


async def test_unknown_source_is_an_error(edge):
    data = await core.get_seo_data("www.example.com", ["nope"])
    assert data["status"] == "error"
    assert "page_speed" in data["reason"]


async def test_exception_text_never_reaches_a_result(edge):
    data = await core.get_seo_data("www.example.com", ["ga4_organic"])
    assert SECRET_TEXT not in json.dumps(data)
    assert data["sources"]["ga4_organic"]["reason"] == "The source failed with RuntimeError."


async def test_top_status_when_nothing_is_connected(edge):
    data = await core.get_seo_data("www.example.com", ["bing_links"])
    assert data["status"] == "not_connected"


async def test_top_status_when_all_fail(edge):
    data = await core.get_seo_data("www.example.com", ["bing_links", "ga4_organic"])
    assert data["status"] == "error"


async def test_timeout_cancels_the_source(edge):
    data = await core.get_seo_data("www.example.com", ["sitemaps"])
    assert data["sources"]["sitemaps"]["status"] == "error"
    assert edge.cancelled == [gsc.HOST]


async def test_output_is_scrubbed(edge):
    def leaky(request):
        dimensions = json.loads(request.content or b"{}").get("dimensions", [])
        if "page" not in dimensions:
            return answer(request)
        keys = [LEAKY_PAGE if name == "page" else f"key-{name}" for name in dimensions]
        row = {"keys": keys, "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}
        return httpx2.Response(200, json={"rows": [row]})

    edge.rules[gsc.HOST] = leaky
    data = await core.get_seo_data("www.example.com", ["search_pages"])
    assert data["sources"]["search_pages"]["status"] == "ok"
    assert "x.test" in json.dumps(data)
    assert "abc123" not in json.dumps(data)


async def test_missing_config_is_not_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_MCP_CONFIG", str(tmp_path / "none.json"))
    data = await core.get_seo_data("www.example.com")
    assert data["status"] == "not_connected"
    assert data["thin_data"] is True


async def test_list_sites_names_no_secret_value(write_config, monkeypatch):
    monkeypatch.setenv("SOME_ENV", "secret-value-99")
    write_config({"gsc_property": "sc-domain:example.com"}, {"t": {"env": "SOME_ENV"}})
    data = core.list_sites()
    assert data["status"] == "ok"
    row = data["rows"][0]
    assert row["host"] == "www.example.com"
    assert row["fields"] == ["gsc_property"]
    assert row["secrets"] == ["t"]
    assert "secret-value-99" not in json.dumps(data)


async def test_a_connection_failure_reads_as_a_sentence(write_config, fake_net, monkeypatch):
    def refuse(request):
        raise httpx2.ConnectError("no route")

    write_config({})
    net.transport = httpx2.MockTransport(refuse)
    data = await core.get_seo_data("www.example.com", ["page_speed"])
    assert data["sources"]["page_speed"]["reason"] == "The connection failed."


async def test_an_http_failure_names_the_status_and_the_api(write_config, fake_net):
    write_config({})
    fake_net(lambda _: httpx2.Response(500))
    data = await core.get_seo_data("www.example.com", ["page_speed"])
    assert data["sources"]["page_speed"]["reason"] == "HTTP 500 from the API."
