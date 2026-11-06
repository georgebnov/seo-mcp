"""A failed call spends once: a repeat inside the window shares the failure and spends nothing."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from seo_mcp import core, net, quota
from seo_mcp.providers import clarity
from test_query_api import HOST
from test_quota import DASHBOARD, INSIGHTS, RECORDINGS, metered_requests, replies, run, set_ledger


def count():
    return quota.view(clarity.LEDGER, HOST)["calls_used_today"]


def saved_file():
    body = {"query": DASHBOARD[1]["query"], "timezone": "UTC"}
    return clarity._body_path(HOST, "dashboard", body)


def mark_file():
    return clarity.failure_path(saved_file())


def clarity_fails(fake_net, status=500):
    def reply(request):
        return httpx2.Response(status) if "clarity" in request.url.host else None

    return fake_net(replies(reply))


class Gate(httpx2.AsyncBaseTransport):
    """Hold each request until the test gives it a reply."""

    def __init__(self):
        self.entered = asyncio.Queue()
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        reply = asyncio.get_running_loop().create_future()
        await self.entered.put(reply)
        return await reply


async def two_callers(first_reply):
    """Caller 1 sends, caller 2 waits for the lock, and then the first call ends."""
    gate = Gate()
    net.transport = gate
    one = asyncio.create_task(run("clarity", DASHBOARD))
    held = await asyncio.wait_for(gate.entered.get(), 5)
    two = asyncio.create_task(run("clarity", DASHBOARD))
    await asyncio.sleep(clarity.LOCK_POLL_SECONDS * 3)
    assert len(gate.requests) == 1
    if isinstance(first_reply, Exception):
        held.set_exception(first_reply)
    else:
        held.set_result(first_reply)
    return one, two, gate


@pytest.mark.parametrize(
    "first", [httpx2.Response(500), httpx2.Response(429), httpx2.ReadTimeout("slow")]
)
async def test_a_waiter_shares_the_failure_and_spends_nothing(live, first):
    one, two, gate = await two_callers(first)
    first_result, second = await one, await asyncio.wait_for(two, 5)
    assert first_result["status"] == second["status"] == "error"
    assert "did not send it again" in second["reason"]
    assert "did not send it again" not in first_result["reason"]
    assert len(gate.requests) == 1
    assert count() == 1
    assert second["calls_used_today"] == 1


async def test_a_connection_that_did_not_open_allows_a_retry(live):
    one, two, gate = await two_callers(httpx2.ConnectError("no route"))
    assert (await one)["status"] == "error"
    again = await asyncio.wait_for(gate.entered.get(), 5)
    again.set_result(httpx2.Response(200, json={"data": []}))
    assert (await two)["status"] == "ok"
    assert count() == 2
    assert not mark_file().exists()


@pytest.mark.parametrize("call", [INSIGHTS, RECORDINGS, DASHBOARD])
async def test_a_repeat_after_the_call_ended_spends_nothing(live, fake_net, call):
    calls = clarity_fails(fake_net)
    await run("clarity", call)
    data = await run("clarity", call)
    assert data["status"] == "error"
    assert "HTTP 500" in data["reason"]
    assert "seconds" in data["reason"]
    assert len(metered_requests(calls)) == 1
    assert count() == 1


async def test_a_repeat_does_not_extend_the_window(live, fake_net):
    clarity_fails(fake_net)
    await run("clarity", DASHBOARD)
    before = mark_file().read_text()
    await run("clarity", DASHBOARD)
    assert mark_file().read_text() == before


async def test_2_pages_of_1_request_share_the_failure(live, fake_net):
    calls = clarity_fails(fake_net)
    await core.get_seo_data(HOST, ["clarity"], page="/a")
    data = await core.get_seo_data(HOST, ["clarity"], page="/b")
    assert data["sources"]["clarity"]["status"] == "error"
    assert len(metered_requests(calls)) == 1


async def test_another_request_is_not_stopped_by_a_failure(live, fake_net):
    calls = clarity_fails(fake_net)
    await run("clarity", DASHBOARD)
    await run("clarity", ("dashboard_query", {"query": "another question"}))
    assert len(metered_requests(calls)) == 2


def age_failure(seconds):
    stored = json.loads(mark_file().read_text())
    at = datetime.now(UTC) - timedelta(seconds=seconds)
    mark_file().write_text(json.dumps({**stored, "failed_at": at.isoformat()}))


async def test_the_request_goes_out_again_after_the_window(live, fake_net):
    calls = clarity_fails(fake_net)
    await run("clarity", DASHBOARD)
    age_failure(clarity.FAILURE_WINDOW_SECONDS)
    await run("clarity", DASHBOARD)
    assert len(metered_requests(calls)) == 2
    assert count() == 2


async def test_a_good_answer_ends_the_failure(live, fake_net):
    clarity_fails(fake_net)
    await run("clarity", DASHBOARD)
    age_failure(clarity.FAILURE_WINDOW_SECONDS)
    fake_net(replies(lambda _: None))
    assert (await run("clarity", DASHBOARD))["status"] == "ok"
    data = await run("clarity", DASHBOARD)
    assert data["status"] == "ok"
    assert data["from_cache"] is True


@pytest.mark.parametrize(
    "stored",
    [
        {"failed_at": "not a time", "status": 500},
        {"failed_at": "2026-01-01T00:00:00", "status": 500},
        {"failed_at": "2099-01-01T00:00:00+00:00", "status": 500},
        {"failed_at": datetime.now(UTC).isoformat(), "status": "500"},
        {"failed_at": datetime.now(UTC).isoformat()},
        [],
    ],
)
async def test_a_broken_failure_mark_is_no_failure(live, stored):
    mark_file().parent.mkdir(parents=True)
    mark_file().write_text(json.dumps(stored))
    assert (await run("clarity", DASHBOARD))["status"] == "ok"
    assert count() == 1


async def test_a_timeout_in_flight_keeps_the_mark(live, monkeypatch):
    class Slow(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(5)

    net.transport = Slow()
    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 0.2)
    assert "timed out" in (await run("clarity", DASHBOARD))["reason"]
    assert "did not send it again" in (await run("clarity", DASHBOARD))["reason"]
    assert count() == 1


async def test_a_timeout_in_the_lock_wait_leaves_no_mark(live, monkeypatch):
    gate = Gate()
    net.transport = gate
    one = asyncio.create_task(run("clarity", DASHBOARD))
    held = await asyncio.wait_for(gate.entered.get(), 5)
    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 0.2)
    assert "timed out" in (await run("clarity", DASHBOARD))["reason"]
    assert not mark_file().exists()
    held.set_result(httpx2.Response(200, json={"data": []}))
    assert (await one)["status"] == "ok"


async def test_a_429_of_the_export_api_still_blocks_the_day(live, fake_net):
    calls = clarity_fails(fake_net, 429)
    insights = ("live_insights", {"numOfDays": 1})
    await run("clarity", insights)
    await run("clarity", insights)
    other = await run("clarity", ("live_insights", {"numOfDays": 2}))
    assert "HTTP 429" in other["reason"]
    assert quota.view(clarity.LEDGER, HOST)["blocked_by_api"] is True
    assert len(metered_requests(calls)) == 1


async def test_at_the_limit_a_repeat_shows_the_failure_and_spends_nothing(live, fake_net):
    calls = clarity_fails(fake_net)
    set_ledger(clarity.LEDGER, HOST, used=9)
    await run("clarity", DASHBOARD)
    data = await run("clarity", DASHBOARD)
    assert "HTTP 500" in data["reason"]
    assert count() == 10
    assert len(metered_requests(calls)) == 1
