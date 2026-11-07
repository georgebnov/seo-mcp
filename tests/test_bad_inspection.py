"""An HTTP 200 of URL Inspection that this server cannot read fails, and a repeat spends nothing."""

import asyncio
import json

import httpx2
import pytest

from seo_mcp import core, net, quota
from seo_mcp.providers import gsc
from test_query_api import HOST, SITE, answer
from test_quota import metered_requests, replies

PAGE = f"https://{HOST}/a"
HOME = f"https://{HOST}/"
GOOD = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
BAD_REPLIES = [
    [],
    ["text"],
    "text",
    None,
    5,
    {},
    {"message": "try later"},
    {"inspectionResult": "text"},
    {"inspectionResult": None},
    {"inspectionResult": ["text"]},
    {"inspectionResult": {"indexStatusResult": "text"}},
    {"inspectionResult": {"indexStatusResult": None}},
    {"inspectionResult": {"indexStatusResult": ["text"]}},
]


def count():
    return quota.view(gsc.LEDGER, SITE["gsc_property"])["calls_used_today"]


def inspected(request):
    return json.loads(request.content)["inspectionUrl"]


def inspection_says(fake_net, reply_of):
    def reply(request):
        if request.url.path.endswith("index:inspect"):
            return httpx2.Response(200, json=reply_of(inspected(request)))
        return None

    return fake_net(replies(reply))


async def index_status(**params):
    data = await core.get_seo_data(HOST, ["index_status"], **params)
    return data["sources"]["index_status"]


@pytest.mark.parametrize("body", BAD_REPLIES)
async def test_a_bad_reply_for_1_page_is_a_clear_failure(live, fake_net, body):
    inspection_says(fake_net, lambda _: body)
    result = await index_status(page=PAGE)
    assert result["status"] == "error"
    assert "shape" in result["reason"]
    assert "Error" not in result["reason"]
    assert result["calls_used_today"] == 1


@pytest.mark.parametrize("body", BAD_REPLIES)
async def test_a_repeat_of_a_bad_reply_sends_nothing(live, fake_net, body):
    calls = inspection_says(fake_net, lambda _: body)
    await index_status(page=PAGE)
    result = await index_status(page=PAGE)
    assert "shape" in result["reason"]
    assert "did not send it again" in result["reason"]
    assert len(metered_requests(calls)) == 1
    assert count() == 1


async def test_a_reply_with_no_inspection_result_is_never_saved(live, fake_net):
    calls = inspection_says(fake_net, lambda _: {"message": "try later"})
    await index_status(page=PAGE)
    inspection_says(fake_net, lambda _: GOOD)
    result = await index_status(page=PAGE)
    assert result["status"] == "error"
    assert "did not send it again" in result["reason"]
    assert len(metered_requests(calls)) == 1


async def test_a_result_with_no_index_status_keeps_its_empty_row(live, fake_net):
    inspection_says(fake_net, lambda _: {"inspectionResult": {"inspectionResultLink": "x"}})
    result = await index_status(page=PAGE)
    assert result["status"] == "ok"
    assert result["rows"][0]["verdict"] is None
    assert result["rows"][0]["coverage_state"] is None


@pytest.mark.parametrize(
    "status",
    [{}, {"verdict": "NEUTRAL", "coverageState": "URL is unknown to Google"}],
    ids=["empty", "unknown url"],
)
@pytest.mark.parametrize("empty", [None, ""], ids=["absent", "empty text"])
async def test_no_canonical_on_either_side_is_no_match_claim(live, fake_net, status, empty):
    fields = {} if empty is None else {"googleCanonical": empty, "userCanonical": empty}
    reply = {"inspectionResult": {"indexStatusResult": {**status, **fields}}}
    inspection_says(fake_net, lambda _: reply)
    result = await index_status(page=PAGE)
    assert result["status"] == "ok"
    assert result["rows"][0]["canonical_match"] is None


async def test_a_batch_of_only_bad_replies_spends_once(live, fake_net):
    calls = inspection_says(fake_net, lambda _: {"inspectionResult": "text"})
    first = await index_status()
    second = await index_status()
    assert "shape" in first["reason"]
    assert "did not send it again" in second["reason"]
    assert second["calls_used_today"] == first["calls_used_today"] == len(metered_requests(calls))


async def test_1_bad_reply_of_a_batch_stays_in_the_saved_answer(live, fake_net):
    calls = inspection_says(fake_net, lambda url: [] if url == HOME else GOOD)
    first = await index_status()
    second = await index_status()
    bad = [row for row in first["rows"] if "error" in row]
    assert first["status"] == "ok"
    assert [row["url"] for row in bad] == [HOME]
    assert "shape" in bad[0]["error"]
    assert second["rows"] == first["rows"]
    assert second["from_cache"] is True
    assert second["calls_used_today"] == len(first["rows"]) == len(metered_requests(calls))


class HoldTheGoodUrl(httpx2.AsyncBaseTransport):
    """Answer the home page at once with a bad reply, and hold each other inspection."""

    def __init__(self):
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0

    async def handle_async_request(self, request):
        if not request.url.path.endswith("index:inspect"):
            return answer(request)
        if inspected(request) == HOME:
            return httpx2.Response(200, json=[])
        self.in_flight += 1
        self.held.set()
        await self.release.wait()
        self.in_flight -= 1
        return httpx2.Response(200, json=GOOD)


async def test_a_bad_reply_does_not_end_the_batch_with_calls_in_flight(live):
    transport = HoldTheGoodUrl()
    net.transport = transport
    job = asyncio.create_task(index_status())
    await asyncio.wait_for(transport.held.wait(), 5)
    await asyncio.sleep(quota.LOCK_POLL_SECONDS * 3)
    ended_early = job.done()
    transport.release.set()
    result = await asyncio.wait_for(job, 5)
    assert not ended_early, "The source ended while inspections of its batch were in flight."
    assert transport.in_flight == 0
    assert result["status"] == "ok"
    assert "1 of 2 URLs failed" in result["reason"]
