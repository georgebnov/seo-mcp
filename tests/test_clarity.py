import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from seo_mcp import core
from seo_mcp.providers import clarity
from test_query_api import clarity_auth

TOKEN = "clarity-token-value-1234"
SITE = {"secrets": {"clarity_token": {"env": "CLARITY_ENV"}}}
LIVE = [
    {
        "metricName": "Traffic",
        "information": [
            {
                "totalSessionCount": "20",
                "distinctUserCount": "4",
                "Url": "https://www.example.com/a",
            },
            {
                "totalSessionCount": "90",
                "distinctUserCount": "9",
                "Url": "https://www.example.com/b",
            },
        ],
    },
    {
        "metricName": "DeadClickCount",
        "information": [
            {"sessionsCount": "3", "pagesViews": "5", "Url": "https://www.example.com/a"}
        ],
    },
]


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", TOKEN)


async def get():
    data = await core.get_seo_data("www.example.com", ["clarity"])
    return data["sources"]["clarity"]


async def test_live_call_sends_the_documented_request(write_config, fake_net):
    write_config(SITE)
    calls = fake_net(lambda _: httpx2.Response(200, json=LIVE))
    result = await get()
    assert result["status"] == "ok"
    assert result["from_cache"] is False
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert calls[0].headers["content-type"] == "application/json"
    assert calls[0].headers["user-agent"] == "seo-mcp"
    assert calls[0].url.params["numOfDays"] == "3"
    assert calls[0].url.params["dimension1"] == "URL"
    assert result["totals"] == {"sessions": 110}
    assert result["thin_data"] is False


async def test_second_call_on_a_utc_day_uses_the_cache(write_config, fake_net):
    write_config(SITE)
    calls = fake_net(lambda _: httpx2.Response(200, json=LIVE))
    first = await get()
    second = await get()
    assert len(calls) == 1
    assert second["from_cache"] is True
    assert second["cached_at"] == first["cached_at"]
    assert second["rows"] == first["rows"]


async def test_cache_of_an_older_day_is_not_served(write_config, fake_net):
    write_config(SITE)
    calls = fake_net(lambda _: httpx2.Response(200, json=LIVE))
    await get()
    path = clarity.cache_path("www.example.com", clarity_auth())
    stored = json.loads(path.read_text())
    stored["cached_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    path.write_text(json.dumps(stored))
    assert (await get())["from_cache"] is False
    assert len(calls) == 2


async def test_broken_cache_is_ignored(write_config, fake_net):
    write_config(SITE)
    calls = fake_net(lambda _: httpx2.Response(200, json=LIVE))
    path = clarity.cache_path("www.example.com", clarity_auth())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert (await get())["status"] == "ok"
    assert len(calls) == 1


async def test_cache_holds_no_token_and_is_private(write_config, fake_net):
    write_config(SITE)
    fake_net(lambda _: httpx2.Response(200, json=LIVE))
    await get()
    path = clarity.cache_path("www.example.com", clarity_auth())
    assert TOKEN not in path.read_text()
    assert path.stat().st_mode & 0o077 == 0


async def test_a_failure_is_not_cached(write_config, fake_net):
    write_config(SITE)
    fake_net(lambda _: httpx2.Response(429))
    result = await get()
    assert result["status"] == "error"
    assert "rate limit" in result["reason"]
    assert not clarity.cache_path("www.example.com", clarity_auth()).exists()


async def test_403_names_a_cut_token_as_a_cause(write_config, fake_net):
    write_config(SITE)
    fake_net(lambda _: httpx2.Response(403))
    result = await get()
    assert result["status"] == "error"
    assert "HTTP 403" in result["reason"]
    assert "invalid or cut" in result["reason"]


async def test_no_token_is_not_connected(write_config):
    write_config()
    assert (await get())["status"] == "not_connected"


async def test_a_changed_project_does_not_read_the_old_answer(write_config, fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json=LIVE))
    write_config({**SITE, "clarity_project": "old-project"})
    assert (await get())["from_cache"] is False
    write_config({**SITE, "clarity_project": "new-project"})
    assert (await get())["from_cache"] is False
    assert len(calls) == 2


async def test_a_changed_project_does_not_read_an_old_dashboard_answer(write_config, fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json={"data": [{"a": 1}]}))
    request = {"query": "q"}
    write_config({**SITE, "clarity_project": "old-project"})
    first = await core.query_api("www.example.com", "clarity", "dashboard_query", request)
    write_config({**SITE, "clarity_project": "new-project"})
    second = await core.query_api("www.example.com", "clarity", "dashboard_query", request)
    assert (first["from_cache"], second["from_cache"]) == (False, False)
    assert len(calls) == 2


async def test_a_site_with_no_project_field_keeps_its_cache_file(write_config, fake_net):
    write_config(SITE)
    fake_net(lambda _: httpx2.Response(200, json=LIVE))
    await get()
    name = clarity.cache_path("www.example.com", clarity_auth()).name
    assert name.startswith("clarity-www.example.com-3d-URL-k2-")
    assert clarity.cache_path("www.example.com", clarity_auth()).exists()
