import httpx2
import pytest

from seo_mcp import core

KEY = "bing-key-value-5678"
SITE = {"bing_site_url": "https://www.example.com/"}
SHARED = {"bing_api_key": {"env": "BING_KEY_ENV"}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("BING_KEY_ENV", KEY)


def links(*pairs, pages=1):
    rows = [{"Url": url, "Count": count} for url, count in pairs]
    return httpx2.Response(200, json={"d": {"Links": rows, "TotalPages": pages}})


async def get(sources=("bing_links",)):
    data = await core.get_seo_data("www.example.com", list(sources))
    return data["sources"]["bing_links"]


async def test_rows_sort_by_count(write_config, fake_net):
    write_config(SITE, SHARED)
    calls = fake_net(
        lambda _: links(("https://www.example.com/a", 3), ("https://www.example.com/b", 40))
    )
    result = await get()
    assert result["status"] == "ok"
    assert result["rows"][0] == {"url": "https://www.example.com/b", "inbound_links": 40}
    assert result["totals"] == {"inbound_links": 43}
    assert result["thin_data"] is False
    assert calls[0].url.path.endswith("/GetLinkCounts")
    assert calls[0].url.params["siteUrl"] == "https://www.example.com/"


async def test_few_links_are_thin(write_config, fake_net):
    write_config(SITE, SHARED)
    fake_net(lambda _: links(("https://www.example.com/a", 3)))
    assert (await get())["thin_data"] is True


async def test_no_links_is_ok_and_says_so(write_config, fake_net):
    write_config(SITE, SHARED)
    fake_net(lambda _: links(pages=0))
    result = await get()
    assert result["status"] == "ok"
    assert result["thin_data"] is True
    assert result["reason"]


async def test_more_pages_are_reported(write_config, fake_net):
    write_config(SITE, SHARED)
    fake_net(lambda _: links(("https://www.example.com/a", 30), pages=4))
    result = await get()
    assert "1 of 4" in result["reason"]


async def test_no_field_is_not_connected(write_config):
    write_config({}, SHARED)
    result = await get()
    assert result["status"] == "not_connected"
    assert "bing_site_url" in result["reason"]


async def test_api_failure_holds_no_key(write_config, fake_net):
    write_config(SITE, SHARED)
    fake_net(lambda request: httpx2.Response(400, text=str(request.url)))
    data = await core.get_seo_data("www.example.com", ["bing_links"])
    assert data["sources"]["bing_links"]["status"] == "error"
    assert KEY not in str(data)
