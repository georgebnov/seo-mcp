import asyncio
import json

import httpx2
import pytest

from seo_mcp import core, net, providers
from seo_mcp.results import NotConnected, ok


async def good(site, params):
    return ok([{"a": 1}, {"a": 2}], thin=False)


async def thin(site, params):
    return ok([], thin=True)


async def absent(site, params):
    raise NotConnected("no field")


async def broken(site, params):
    raise RuntimeError("secret-text-from-an-exception")


async def api_down(site, params):
    raise net.ApiError(403)


async def slow(site, params):
    await asyncio.sleep(30)


@pytest.fixture
def sources(monkeypatch, write_config):
    write_config()
    table = {
        "good": good,
        "thin": thin,
        "absent": absent,
        "broken": broken,
        "api_down": api_down,
        "slow": slow,
    }
    monkeypatch.setattr(providers, "SOURCES", table)
    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 0.05)
    return table


async def test_only_a_site_runs_every_source(sources):
    data = await core.get_seo_data("www.example.com")
    assert set(data["sources"]) == set(sources)
    assert data["status"] == "ok"
    assert data["sources"]["good"]["rows"] == [{"a": 1}, {"a": 2}]
    assert data["sources"]["absent"]["status"] == "not_connected"
    assert data["sources"]["api_down"]["status"] == "error"
    assert data["sources"]["slow"]["status"] == "error"
    assert data["thin_data"] is True
    assert sorted(data["thin_sources"]) == ["absent", "api_down", "broken", "slow", "thin"]
    assert data["counts"] == {"ok": 2, "not_connected": 1, "error": 3}


async def test_sources_limits_the_call(sources):
    data = await core.get_seo_data("www.example.com", ["good"])
    assert list(data["sources"]) == ["good"]
    assert data["thin_data"] is False


async def test_unknown_source_is_an_error(sources):
    data = await core.get_seo_data("www.example.com", ["nope"])
    assert data["status"] == "error"
    assert "good" in data["reason"]


async def test_exception_text_never_reaches_a_result(sources):
    data = await core.get_seo_data("www.example.com", ["broken"])
    assert "secret-text" not in json.dumps(data)
    assert data["sources"]["broken"]["reason"]


async def test_top_status_when_nothing_is_connected(sources):
    data = await core.get_seo_data("www.example.com", ["absent"])
    assert data["status"] == "not_connected"


async def test_top_status_when_all_fail(sources):
    data = await core.get_seo_data("www.example.com", ["absent", "broken"])
    assert data["status"] == "error"


async def test_timeout_cancels_the_source(sources, monkeypatch):
    seen = []

    async def waits(site, params):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    monkeypatch.setitem(sources, "waits", waits)
    await core.get_seo_data("www.example.com", ["waits"])
    assert seen == ["cancelled"]


async def test_output_is_scrubbed(sources, monkeypatch):
    async def leaky(site, params):
        return ok([{"url": "https://x.test/?apikey=abc123"}], thin=False)

    monkeypatch.setitem(sources, "leaky", leaky)
    data = await core.get_seo_data("www.example.com", ["leaky"])
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
