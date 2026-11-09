"""A recordings answer fits the tool output of an agent: each row keeps only the useful fields."""

import json

import httpx2
import pytest

from seo_mcp import core
from seo_mcp.providers import clarity
from test_query_api import HOST, SITE, answer

DAY = {"start": "2026-01-01T00:00:00.000Z", "end": "2026-01-01T23:59:59.999Z"}
FBCLID = "IwAR" + "Zx9" * 60
# Each key that a trimmed row can hold, at each level.
KEPT = {"timestamp", "link", "timeline", "url", "timelineEvents", "eventtype", "hash", "text"}
# The tool output of an agent cut a real answer of 68,321 characters. The 20 newest real
# sessions trim to about 15,700 characters with the answer fields. 20,000 gives heavier
# sessions room and keeps the dashboard step and the work of the agent next to it.
DEFAULT_BUDGET = 20_000


def event(i, j, k):
    """1 event as Clarity sends it. link is always empty in real answers."""
    kinds = ("Click", "Dead click", "Click", "Entered Text")
    texts = ("Get started", "Pricing", "", "Book a free demo today")
    return {
        "eventtype": kinds[k % 4],
        "hash": f"h{i}p{j}e{k}",
        "link": "",
        "start": f"00:0{j}:{10 + k}",
        "text": texts[(i + k) % 4],
    }


def page(i, j, events=3):
    """1 timeline page as Clarity sends it, with ad tokens in the URL and the referrer."""
    url = f"https://{HOST}/page-{j}"
    return {
        "CLS": "0.0123",
        "FID": "16.0001",
        "LCP": "2.4",
        "displayTitle": "Example product page with a long and full title",
        "duration": 41000 + j,
        "pageLoadTime": 1200,
        "referrerUrl": f"https://l.facebook.com/l.php?u=x&fbclid={FBCLID}" if j == 0 else url,
        "score": 90,
        "start": f"0{j}:12",
        "url": f"{url}?utm_source=facebook&fbclid={FBCLID}#top" if j == 0 else url,
        "timelineEvents": [event(i, j, k) for k in range(events)],
    }


def session(i, pages=3, events=2):
    """A real session has about 3 pages and 5 events."""
    return {
        "activeDuration": "41 seconds and 2 minutes",
        "totalDuration": "12 seconds and 4 minutes",
        "link": f"https://clarity.microsoft.com/player/proj123/s{i:05d}/p1",
        "pagesCount": pages,
        "sessionClickCount": pages * events,
        "timestamp": f"2026-01-01 1{i % 10}:2{i % 6}:0{i % 10}",
        "timeline": [page(i, j, events) for j in range(pages)],
    }


def sample(make=session):
    """Answer a recordings request with as many sessions as it asks for."""

    def reply(request):
        if request.url.path.endswith("/mcp/recordings/sample"):
            count = json.loads(request.content)["count"]
            return httpx2.Response(200, json=[make(i) for i in range(count)])
        return answer(request)

    return reply


@pytest.fixture
def live(write_config, fake_net, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    write_config(SITE)
    return fake_net


async def recordings(request):
    return await core.query_api(HOST, "clarity", "recordings", request)


def keys(value):
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in keys(item)}
    return set()


async def test_a_row_keeps_the_session_time_the_link_the_page_and_each_event(live):
    live(sample(lambda i: session(i, 3, 3)))
    data = await recordings({"filters": {"date": DAY}, "count": 1})
    assert data["rows"] == [
        {
            "timestamp": "2026-01-01 10:20:00",
            "link": "https://clarity.microsoft.com/player/proj123/s00000/p1",
            "timeline": [
                {
                    "url": f"https://{HOST}/page-{j}",
                    "timelineEvents": [
                        {"eventtype": "Click", "hash": f"h0p{j}e0", "text": "Get started"},
                        {"eventtype": "Dead click", "hash": f"h0p{j}e1", "text": "Pricing"},
                        {"eventtype": "Click", "hash": f"h0p{j}e2", "text": ""},
                    ],
                }
                for j in range(3)
            ],
        }
    ]
    assert "fbclid" not in json.dumps(data)
    assert "timelineEvents" in data["kept_fields"]


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        pytest.param(
            "https://user:pw@WWW.example.com:8443/a/b?x=1#y", "https://www.example.com:8443/a/b"
        ),
        pytest.param("https://www.example.com/?fbclid=abc", "https://www.example.com/"),
        pytest.param("/relative/path?q=1", "/relative/path"),
        pytest.param(None, None),
    ],
)
async def test_a_page_url_keeps_no_query_no_fragment_and_no_login(live, url, kept):
    live(sample(lambda i: {**session(i, 1, 0), "timeline": [{**page(i, 0, 0), "url": url}]}))
    data = await recordings({"filters": {"date": DAY}, "count": 1})
    assert data["rows"][0]["timeline"] == [{"url": kept, "timelineEvents": []}]


async def test_a_default_answer_asks_20_sessions_and_fits_the_budget(live):
    calls = live(sample())
    data = await recordings({"filters": {"date": DAY, "deadClickPresent": True}})
    (call,) = [call for call in calls if call.url.host == "clarity.microsoft.com"]
    assert json.loads(call.content)["count"] == 20
    raw = len(json.dumps([session(i) for i in range(20)]))
    size = len(json.dumps(data))
    assert data["rows_returned"] == 20
    assert size < DEFAULT_BUDGET, f"The default answer has {size} characters, raw {raw}."
    assert raw > 2 * DEFAULT_BUDGET, "The fixture must be as heavy as a real answer."


async def test_a_large_answer_stops_at_whole_sessions_and_says_so(live):
    live(sample(lambda i: session(i, 6, 12)))
    data = await recordings({"filters": {"date": DAY}, "count": 250})
    assert len(json.dumps(data["rows"])) <= clarity.ROWS_BUDGET
    assert 1 < data["rows_returned"] < 250
    assert data["sessions_fetched"] == 250
    assert data["more_rows"] == "possible"
    assert str(data["rows_returned"]) in data["reason"]


async def test_1_session_over_the_budget_still_shows(live):
    live(sample(lambda i: session(i, 20, 40)))
    data = await recordings({"filters": {"date": DAY}, "count": 2})
    assert data["rows_returned"] == 1
    assert data["sessions_fetched"] == 2


def extra_at_each_level(i):
    row = session(i, 2, 2)
    row["timeline"][0]["timelineEvents"][0]["newEventField"] = "x"
    row["timeline"][0]["newPageField"] = {"deep": "x"}
    row["timeline"][1]["url"] = {"nested": "x"}
    row["timeline"][1]["timelineEvents"] = "not a list"
    return {**row, "newSessionField": [{"deep": "x"}], "timestamp": {"nested": "x"}}


async def test_no_clarity_field_outside_the_list_reaches_an_answer(live):
    """The guard: a new or nested Clarity field must not pass through, also from the cache."""
    live(sample(extra_at_each_level))
    request = {"filters": {"date": DAY}, "count": 3}
    for data in (await recordings(request), await recordings(request)):
        leaked = keys(data["rows"]) - KEPT
        assert not leaked, (
            f"Recordings rows hold {sorted(leaked)}. In src/seo_mcp/providers/clarity.py, pass "
            "each row through _trim with RECORDING_FIELDS before answer()."
        )
        assert data["rows_returned"] == 3
    assert data["from_cache"] is True
