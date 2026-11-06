"""A stuck ledger lock refuses a metered call in a short time and stops no other source."""

import ast
import asyncio
import fcntl
import threading
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, quota, shared
from seo_mcp.providers import clarity, ga4, gsc
from test_query_api import HOST, SITE
from test_quota import DASHBOARD, GA4_QUOTA, INSPECT, metered_requests, replies, run, sent

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
WAIT = 0.2
HOLD = 1.0


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(quota, "LOCK_WAIT_SECONDS", WAIT)


def hold_lock(target, seconds=HOLD):
    """A stuck process: another open file holds the lock, and a thread lets it go at the end."""
    lock_file = target.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    timer = threading.Timer(seconds, handle.close)
    timer.start()
    return timer


@pytest.mark.parametrize(
    ("api", "call", "ledger", "key"),
    [
        ("clarity", DASHBOARD, clarity.LEDGER, HOST),
        ("search_console", INSPECT, gsc.LEDGER, SITE["gsc_property"]),
    ],
)
async def test_a_stuck_ledger_refuses_and_sends_nothing(live, short_wait, api, call, ledger, key):
    timer = hold_lock(quota.path(ledger, key))
    data = await run(api, call)
    timer.join()
    assert data["status"] == "error"
    assert "busy" in data["reason"]
    assert quota.path(ledger, key).name in data["reason"]
    assert not metered_requests(live)
    assert quota.view(ledger, key)["calls_used_today"] == 0


async def test_a_stuck_ledger_stops_no_other_source(live, short_wait):
    timer = hold_lock(quota.path(clarity.LEDGER, HOST))
    data = await core.get_seo_data(HOST, ["clarity", "search_queries"])
    timer.join()
    assert data["sources"]["clarity"]["status"] == "error"
    assert data["sources"]["search_queries"]["status"] == "ok"


async def test_a_stuck_ledger_does_not_freeze_the_event_loop(live, short_wait):
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    timer = hold_lock(quota.path(clarity.LEDGER, HOST))
    await asyncio.sleep(0)
    ticks.clear()
    await run("clarity", DASHBOARD)
    count = len(ticks)
    beat.cancel()
    timer.join()
    assert count > 5


async def test_a_short_hold_only_delays_the_call(live, monkeypatch):
    monkeypatch.setattr(quota, "LOCK_WAIT_SECONDS", 2)
    timer = hold_lock(quota.path(clarity.LEDGER, HOST), 0.2)
    data = await run("clarity", DASHBOARD)
    timer.join()
    assert data["status"] == "ok"
    assert data["calls_used_today"] == 1


async def test_list_sites_does_not_wait_for_a_stuck_ledger(live, short_wait):
    timer = hold_lock(quota.path(clarity.LEDGER, HOST))
    assert core.list_sites()["status"] == "ok"
    timer.join()


async def test_a_stuck_ledger_after_a_429_keeps_the_429_reason(live, fake_net, short_wait):
    def reply(request):
        if request.url.host == "www.clarity.ms":
            timers.append(hold_lock(quota.path(clarity.LEDGER, HOST)))
            return httpx2.Response(429)
        return None

    timers = []
    fake_net(replies(reply))
    data = await run("clarity", ("live_insights", {"numOfDays": 1}))
    timers[0].join()
    assert "HTTP 429" in data["reason"]
    assert data["calls_used_today"] == 1
    assert shared.failure_path(clarity.cache_path(HOST, 1, ())).exists()


async def test_a_stuck_reading_file_does_not_lose_a_paid_ga4_answer(live, fake_net, short_wait):
    def reply(request):
        if request.url.path.endswith(":runReport"):
            body = {"rows": [], "propertyQuota": GA4_QUOTA}
            return httpx2.Response(200, json=body)
        return None

    calls = fake_net(replies(reply))
    timer = hold_lock(ga4.reading_path(SITE["ga4_property_id"], "core"))
    data = await run("ga4", ("run_report", {"metrics": [{"name": "sessions"}]}))
    timer.join()
    assert data["status"] == "ok"
    assert len(sent(calls)) == 1


def _blocking_flocks(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "fcntl.flock":
            if "LOCK_NB" not in ast.unparse(node):
                yield node.lineno


def test_no_module_takes_a_blocking_lock():
    found = [
        f"{path.name}:{line}"
        for path in sorted(SOURCE.rglob("*.py"))
        for line in _blocking_flocks(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not found, f"A blocking flock freezes each source. Use quota.locked: {found}"
