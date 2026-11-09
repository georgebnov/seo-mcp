"""A failed call spends once: a repeat inside the window shares the failure and spends nothing."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from seo_mcp import core, net, quota, shared
from seo_mcp.providers import clarity, gsc
from test_query_api import HOST, SITE, clarity_auth
from test_quota import (
    DASHBOARD,
    INSIGHTS,
    INSPECT,
    RECORDINGS,
    metered_requests,
    replies,
    run,
    sent,
    set_ledger,
)


def count():
    return quota.view(clarity.LEDGER, HOST)["calls_used_today"]


def saved_file():
    body = {"query": DASHBOARD[1]["query"], "timezone": "UTC"}
    return clarity._body_path(HOST, "dashboard", body, clarity_auth())


def mark_file():
    return shared.failure_path(saved_file())


def clarity_fails(fake_net, status=500):
    def reply(request):
        return httpx2.Response(status) if "clarity" in request.url.host else None

    return fake_net(replies(reply))


def connection_fails(fake_net, host):
    def reply(request):
        if host in request.url.host:
            raise httpx2.ConnectError("no route")

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
    await asyncio.sleep(quota.LOCK_POLL_SECONDS * 3)
    assert len(gate.requests) == 1
    if isinstance(first_reply, Exception):
        held.set_exception(first_reply)
    else:
        held.set_result(first_reply)
    return one, two, gate


@pytest.mark.parametrize(
    "first",
    [
        httpx2.Response(500),
        httpx2.Response(429),
        httpx2.ReadTimeout("slow"),
        httpx2.ConnectError("no route"),
    ],
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


@pytest.mark.parametrize("call", [INSIGHTS, RECORDINGS, DASHBOARD])
async def test_a_repeat_of_a_connection_failure_spends_nothing(live, fake_net, call):
    calls = connection_fails(fake_net, "clarity")
    first = await run("clarity", call)
    second = await run("clarity", call)
    assert first["reason"].startswith("The connection failed.")
    assert "did not send it again" not in first["reason"]
    assert second["reason"].startswith("The connection failed.")
    assert "did not send it again" in second["reason"]
    assert len(metered_requests(calls)) == 1
    assert count() == second["calls_used_today"] == 1


async def test_a_repeat_of_a_failed_url_inspection_spends_nothing(live, fake_net):
    calls = connection_fails(fake_net, "searchconsole")
    await run("search_console", INSPECT)
    second = await run("search_console", INSPECT)
    assert "did not send it again" in second["reason"]
    assert len(sent(calls)) == 1
    assert quota.view(gsc.LEDGER, SITE["gsc_property"])["calls_used_today"] == 1


async def test_a_repeat_of_a_failed_batch_spends_nothing(live, fake_net):
    def reply(request):
        if request.url.path.endswith("index:inspect"):
            raise httpx2.ConnectError("no route")

    calls = fake_net(replies(reply))
    first = (await core.get_seo_data(HOST, ["index_status"]))["sources"]["index_status"]
    tries = len(sent(calls))
    second = (await core.get_seo_data(HOST, ["index_status"]))["sources"]["index_status"]
    assert first["calls_used_today"] > 1
    assert "did not send it again" in second["reason"]
    assert second["calls_used_today"] == first["calls_used_today"]
    assert len(sent(calls)) == tries


async def test_a_connection_failure_counts_again_only_after_the_window(live, fake_net):
    calls = connection_fails(fake_net, "clarity")
    await run("clarity", DASHBOARD)
    age_failure(shared.FAILURE_WINDOW_SECONDS)
    await run("clarity", DASHBOARD)
    assert len(metered_requests(calls)) == 2
    assert count() == 2


async def test_another_request_is_not_stopped_by_a_connection_failure(live, fake_net):
    calls = connection_fails(fake_net, "clarity")
    await run("clarity", DASHBOARD)
    await run("clarity", ("dashboard_query", {"query": "another question"}))
    assert len(metered_requests(calls)) == 2
    assert count() == 2


async def test_a_read_with_no_ledger_retries_a_connection_failure_at_once(live, fake_net):
    calls = connection_fails(fake_net, "googleapis.com")
    await core.get_seo_data(HOST, ["page_speed"])
    second = (await core.get_seo_data(HOST, ["page_speed"]))["sources"]["page_speed"]
    assert second["reason"] == "The connection failed."
    assert len(calls) == 2


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
    age_failure(shared.FAILURE_WINDOW_SECONDS)
    await run("clarity", DASHBOARD)
    assert len(metered_requests(calls)) == 2
    assert count() == 2


async def test_a_good_answer_ends_the_failure(live, fake_net):
    clarity_fails(fake_net)
    await run("clarity", DASHBOARD)
    age_failure(shared.FAILURE_WINDOW_SECONDS)
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
    mark_file().parent.mkdir(parents=True, exist_ok=True)
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


async def test_after_the_429_no_new_call_spends(live, monkeypatch):
    """2 calls that reserved before the 429 both go out. A call after the 429 spends nothing."""
    gate = Gate()
    net.transport = gate
    paths = [("live_insights", {"numOfDays": days}) for days in (1, 2, 3)]
    one = asyncio.create_task(run("clarity", paths[0]))
    first = await asyncio.wait_for(gate.entered.get(), 5)
    two = asyncio.create_task(run("clarity", paths[1]))
    second = await asyncio.wait_for(gate.entered.get(), 5)
    first.set_result(httpx2.Response(429))
    assert (await one)["blocked_by_api"] is True
    second.set_result(httpx2.Response(429))
    assert "HTTP 429" in (await two)["reason"]
    # A call that the ledger lets through waits at the gate: stop it soon.
    monkeypatch.setattr(core, "SOURCE_TIMEOUT_SECONDS", 1)
    late = await run("clarity", paths[2])
    assert "HTTP 429" in late["reason"]
    assert len(gate.requests) == 2
    assert count() == late["calls_used_today"] == 2


async def test_at_the_limit_a_repeat_shows_the_failure_and_spends_nothing(live, fake_net):
    calls = clarity_fails(fake_net)
    set_ledger(clarity.LEDGER, HOST, used=9)
    await run("clarity", DASHBOARD)
    data = await run("clarity", DASHBOARD)
    assert "HTTP 500" in data["reason"]
    assert count() == 10
    assert len(metered_requests(calls)) == 1
