import httpx2
import pytest

from seo_mcp import core

KEY = "google-key-value-1234"
SHARED = {"google_api_key": {"env": "GOOGLE_KEY_ENV"}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("GOOGLE_KEY_ENV", KEY)


async def get(name):
    data = await core.get_seo_data("www.example.com", [name])
    return data["sources"][name]


PSI = {
    "lighthouseResult": {
        "fetchTime": "2026-01-01T00:00:00Z",
        "categories": {"performance": {"score": 0.91}, "seo": {"score": 1}},
        "audits": {
            "largest-contentful-paint": {
                "title": "LCP",
                "numericValue": 1800.5,
                "displayValue": "1.8 s",
            },
            "unused-javascript": {
                "title": "Reduce unused JavaScript",
                "details": {"type": "opportunity", "overallSavingsMs": 300},
            },
        },
    }
}


async def test_page_speed_rows_and_key_in_a_header(write_config, fake_net):
    write_config({}, SHARED)
    calls = fake_net(lambda _: httpx2.Response(200, json=PSI))
    result = await get("page_speed")
    assert result["status"] == "ok"
    assert {"kind": "score", "metric": "performance", "value": 91} in result["rows"]
    assert {
        "kind": "lab",
        "metric": "largest-contentful-paint",
        "value": 1800.5,
        "display": "1.8 s",
    } in result["rows"]
    assert {
        "kind": "opportunity",
        "metric": "unused-javascript",
        "value": 300,
        "display": "Reduce unused JavaScript",
    } in result["rows"]
    assert calls[0].headers["x-goog-api-key"] == KEY
    assert KEY not in str(calls[0].url)
    assert calls[0].url.params["url"] == "https://www.example.com/"


async def test_page_speed_runs_without_a_key(write_config, fake_net):
    write_config()
    calls = fake_net(lambda _: httpx2.Response(200, json=PSI))
    assert (await get("page_speed"))["status"] == "ok"
    assert "x-goog-api-key" not in calls[0].headers


CRUX = {
    "record": {
        "metrics": {
            "largest_contentful_paint": {"percentiles": {"p75": 2600}},
            "cumulative_layout_shift": {"percentiles": {"p75": "0.05"}},
            "form_factors": {"fractions": {"phone": 1}},
        },
        "collectionPeriod": {"firstDate": {"year": 2026}, "lastDate": {"year": 2026}},
    }
}


async def test_chrome_field_data(write_config, fake_net):
    write_config({}, SHARED)
    calls = fake_net(lambda _: httpx2.Response(200, json=CRUX))
    result = await get("chrome_field_data")
    assert {"metric": "largest_contentful_paint", "p75": 2600, "rating": "needs-improvement"} in (
        result["rows"]
    )
    assert {"metric": "cumulative_layout_shift", "p75": 0.05, "rating": "good"} in result["rows"]
    assert len(result["rows"]) == 2
    assert b"https://www.example.com" in calls[0].content


async def test_no_field_data_is_ok_and_thin(write_config, fake_net):
    write_config({}, SHARED)
    fake_net(lambda _: httpx2.Response(404))
    result = await get("chrome_field_data")
    assert result["status"] == "ok"
    assert result["thin_data"] is True
    assert result["reason"]


async def test_field_data_needs_a_key(write_config):
    write_config()
    assert (await get("chrome_field_data"))["status"] == "not_connected"
