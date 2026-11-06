"""An HTTP 200 of a wrong shape is a failure: it is never saved, and it spends 1 call."""

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from seo_mcp import core, quota
from seo_mcp.providers import clarity
from test_query_api import HOST
from test_quota import (
    DASHBOARD,
    INSIGHTS,
    RECORDINGS,
    metered_requests,
    replies,
    run,
    set_ledger,
)

METRICS = [{"metricName": "Traffic", "information": [{"Url": "u", "totalSessionCount": "7"}]}]
BAD_INSIGHTS = [
    {"message": "try later"},
    "text",
    None,
    ["text"],
    [{"metricName": "Traffic", "information": ["text"]}],
    [{"metricName": "Traffic", "information": {"a": 1}}],
]
BAD_DASHBOARD = [{"message": "try later"}, [], "text", {"data": "text"}, {"data": ["text"]}]
BAD_RECORDINGS = [{"message": "try later"}, "text", ["text"]]


def count():
    return quota.view(clarity.LEDGER, HOST)["calls_used_today"]


def clarity_says(fake_net, body):
    def reply(request):
        return httpx2.Response(200, json=body) if "clarity" in request.url.host else None

    return fake_net(replies(reply))


def saved_files():
    names = quota.folder().glob("clarity-*.json")
    return [path for path in names if "failed" not in path.name and "-calls-" not in path.name]


@pytest.mark.parametrize("body", BAD_INSIGHTS)
async def test_a_bad_answer_of_the_source_is_a_clear_failure(live, fake_net, body):
    clarity_says(fake_net, body)
    data = await core.get_seo_data(HOST, ["clarity"])
    result = data["sources"]["clarity"]
    assert result["status"] == "error"
    assert "shape" in result["reason"]
    assert "Error" not in result["reason"]
    assert result["calls_used_today"] == 1
    assert not saved_files()


@pytest.mark.parametrize(
    ("call", "body"),
    [(INSIGHTS, body) for body in BAD_INSIGHTS]
    + [(DASHBOARD, body) for body in BAD_DASHBOARD]
    + [(RECORDINGS, body) for body in BAD_RECORDINGS],
)
async def test_a_bad_answer_of_each_method_is_never_saved(live, fake_net, call, body):
    clarity_says(fake_net, body)
    data = await run("clarity", call)
    assert data["status"] == "error"
    assert "shape" in data["reason"]
    assert not saved_files()


async def test_a_repeat_of_a_bad_answer_keeps_the_reason_and_spends_nothing(live, fake_net):
    calls = clarity_says(fake_net, {"message": "try later"})
    await core.get_seo_data(HOST, ["clarity"])
    result = (await core.get_seo_data(HOST, ["clarity"]))["sources"]["clarity"]
    assert "shape" in result["reason"]
    assert "did not send it again" in result["reason"]
    assert len(metered_requests(calls)) == 1
    assert count() == 1


async def test_the_source_works_again_after_the_window(live, fake_net):
    clarity_says(fake_net, {"message": "try later"})
    await core.get_seo_data(HOST, ["clarity"])
    mark = clarity.failure_path(clarity.cache_path(HOST))
    old = datetime.now(UTC) - timedelta(seconds=clarity.FAILURE_WINDOW_SECONDS)
    mark.write_text(json.dumps({**json.loads(mark.read_text()), "failed_at": old.isoformat()}))
    clarity_says(fake_net, METRICS)
    result = (await core.get_seo_data(HOST, ["clarity"]))["sources"]["clarity"]
    assert result["status"] == "ok"
    assert result["totals"]["sessions"] == 7
    assert count() == 2


def save_today(body):
    path = clarity.cache_path(HOST)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cached_at": datetime.now(UTC).isoformat(), "answer": body}))


async def test_a_bad_answer_that_an_old_server_saved_is_not_served(live, fake_net):
    calls = clarity_says(fake_net, METRICS)
    save_today({"message": "try later"})
    result = (await core.get_seo_data(HOST, ["clarity"]))["sources"]["clarity"]
    assert result["status"] == "ok"
    assert result["from_cache"] is False
    assert len(metered_requests(calls)) == 1


async def test_at_the_limit_an_old_bad_answer_gives_the_limit_reason(live):
    set_ledger(clarity.LEDGER, HOST, used=10)
    save_today({"message": "try later"})
    result = (await core.get_seo_data(HOST, ["clarity"]))["sources"]["clarity"]
    assert "10 calls" in result["reason"]
    assert not metered_requests(live)


@pytest.mark.parametrize(
    ("call", "body"),
    [
        (INSIGHTS, [{"metricName": "Traffic", "information": []}]),
        (INSIGHTS, [{"metricName": "Traffic"}]),
        (INSIGHTS, []),
        (DASHBOARD, {"query": "q", "data": [{"Device": "PC"}], "dataErrorType": 0}),
        (DASHBOARD, {"query": "q", "data": [], "dataErrorType": 3}),
        (DASHBOARD, {"query": "q", "data": None, "dataErrorType": 3}),
        (RECORDINGS, []),
        (RECORDINGS, [{"link": "l", "pagesCount": 1}]),
    ],
)
async def test_a_good_answer_of_each_method_is_saved(live, fake_net, call, body):
    clarity_says(fake_net, body)
    assert (await run("clarity", call))["status"] == "ok"
    assert len(saved_files()) == 1
    assert (await run("clarity", call))["from_cache"] is True
