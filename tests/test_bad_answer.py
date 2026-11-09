"""An HTTP 200 of a wrong shape is a failure: it is never saved, and it spends 1 call."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    set_ledger,
)

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"

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
    mark = shared.failure_path(clarity.cache_path(HOST, clarity_auth()))
    old = datetime.now(UTC) - timedelta(seconds=shared.FAILURE_WINDOW_SECONDS)
    mark.write_text(json.dumps({**json.loads(mark.read_text()), "failed_at": old.isoformat()}))
    clarity_says(fake_net, METRICS)
    result = (await core.get_seo_data(HOST, ["clarity"]))["sources"]["clarity"]
    assert result["status"] == "ok"
    assert result["totals"]["sessions"] == 7
    assert count() == 2


def save_today(body):
    path = clarity.cache_path(HOST, clarity_auth())
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


# Each path that spends: a reply of any shape must not let a repeat spend again.
JUNK = [
    [],
    ["text"],
    "text",
    None,
    5,
    {},
    {"message": "try later"},
    {"data": "text"},
    {"inspectionResult": "text"},
    {"inspectionResult": None},
    {"inspectionResult": ["text"]},
    {"inspectionResult": {"indexStatusResult": "text"}},
]
CLARITY = (clarity.LEDGER, HOST)
INSPECTION = (gsc.LEDGER, SITE["gsc_property"])


def source(name, **params):
    return lambda: core.get_seo_data(HOST, [name], **params)


def method(api, call):
    return lambda: run(api, call)


SPENDERS = {
    ("clarity.py", "_cached"): [
        ("clarity", CLARITY, source("clarity")),
        ("live_insights", CLARITY, method("clarity", INSIGHTS)),
        ("recordings", CLARITY, method("clarity", RECORDINGS)),
        ("dashboard_query", CLARITY, method("clarity", DASHBOARD)),
    ],
    ("gsc.py", "_url_inspection"): [
        ("url_inspection", INSPECTION, method("search_console", INSPECT)),
    ],
    ("gsc.py", "index_status"): [
        ("index_status of 1 page", INSPECTION, source("index_status", page=f"https://{HOST}/a")),
        ("index_status of a batch", INSPECTION, source("index_status")),
    ],
}
PATHS = [path for paths in SPENDERS.values() for path in paths]


def spends(function):
    called = (ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call))
    return "quota.spend" in called


def spenders():
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and spends(node):
                yield path.name, node.name


@pytest.mark.parametrize("body", JUNK, ids=json.dumps)
@pytest.mark.parametrize(("name", "spent", "call"), PATHS, ids=[path[0] for path in PATHS])
async def test_a_repeat_of_any_reply_sends_nothing(live, fake_net, name, spent, call, body):
    def reply(request):
        if metered_requests([request]):
            return httpx2.Response(200, json=body)
        return None

    calls = fake_net(replies(reply))
    await call()
    first = len(metered_requests(calls))
    await call()
    assert first > 0
    assert len(metered_requests(calls)) == first, f"{name} sent the repeat of a spent call."
    assert quota.view(*spent)["calls_used_today"] == first


INSPECTIONS = [path for key, paths in SPENDERS.items() if key[0] == "gsc.py" for path in paths]
NO_RESULT = [{}, {"message": "try later"}, {"inspectionResult": "text"}]


@pytest.mark.parametrize("body", NO_RESULT, ids=json.dumps)
@pytest.mark.parametrize(("name", "_", "call"), INSPECTIONS, ids=[path[0] for path in INSPECTIONS])
async def test_no_inspection_result_is_a_failure_on_each_path(live, fake_net, name, _, call, body):
    def reply(request):
        return httpx2.Response(200, json=body) if metered_requests([request]) else None

    fake_net(replies(reply))
    data = await call()
    result = data.get("sources", {}).get("index_status", data)
    fix = "Require an inspectionResult object before the path shows or saves a reply."
    assert result["status"] == "error", f"gsc.py: {name} showed a failed call as ok. {fix}"
    assert "shape" in result["reason"]


READERS = ("shared.request", "shared.once")
# index_status builds its own answer and checks each URL in _index_status.
NO_SHAPE_CHECK = {("gsc.py", "index_status")}


def unchecked_reads():
    def visit(node, name, file):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = node.name
        if isinstance(node, ast.Call) and ast.unparse(node.func) in READERS:
            if "good" not in {keyword.arg for keyword in node.keywords}:
                yield file, name
        for child in ast.iter_child_nodes(node):
            yield from visit(child, name, file)

    paths = sorted((SOURCE / "providers").glob("*.py"))
    return {
        pair
        for path in paths
        for pair in visit(ast.parse(path.read_text(encoding="utf-8")), "module", path.name)
    }


def test_each_function_that_spends_is_in_the_sweep():
    wrong = sorted(set(spenders()) ^ set(SPENDERS))
    assert not wrong, f"Add each path of these functions to SPENDERS in {__file__}: {wrong}"


# A live read of a date range with no data answered with this body, and with no rows field.
REAL_EMPTY = {"responseAggregationType": "byProperty"}
RANGE = {"startDate": "2026-01-01", "endDate": "2026-01-02"}


def search_console(name, request):
    return lambda: core.query_api(HOST, "search_console", name, request)


READS = [
    ("search_queries", source("search_queries"), "/searchAnalytics/query"),
    ("search_pages", source("search_pages"), "/searchAnalytics/query"),
    ("quick_wins", source("quick_wins"), "/searchAnalytics/query"),
    ("sitemaps", source("sitemaps"), "/sitemaps"),
    ("search_analytics", search_console("search_analytics", RANGE), "/searchAnalytics/query"),
    ("sitemap list", search_console("sitemaps", {}), "/sitemaps"),
]
READ_IDS = [read[0] for read in READS]


def result_of(data, name):
    return data.get("sources", {}).get(name, data)


def search_console_says(fake_net, ending, body):
    def reply(request):
        return httpx2.Response(200, json=body) if request.url.path.endswith(ending) else None

    return fake_net(replies(reply))


def sent(calls, ending):
    return [call for call in calls if call.url.path.endswith(ending)]


@pytest.mark.parametrize(("name", "call", "ending"), READS, ids=READ_IDS)
async def test_a_search_console_reply_of_no_known_field_is_a_failure(
    live, fake_net, name, call, ending
):
    search_console_says(fake_net, ending, {"message": "try later"})
    result = result_of(await call(), name)
    assert result["status"] == "error", f"{name} showed a failed read as an ok empty result."
    assert "shape" in result["reason"]
    assert result["rows"] == []


@pytest.mark.parametrize(("name", "call", "ending"), READS, ids=READ_IDS)
async def test_a_bad_search_console_reply_is_never_saved(live, fake_net, name, call, ending):
    calls = search_console_says(fake_net, ending, {"message": "try later"})
    await call()
    first = len(sent(calls, ending))
    result = result_of(await call(), name)
    assert result["status"] == "error"
    assert "did not send it again" in result["reason"]
    assert len(sent(calls, ending)) == first
    assert not [
        path
        for path in shared.folder().glob("*.json")
        if "try later" in path.read_text() and "failed" not in path.name
    ]


async def test_a_real_empty_answer_of_search_analytics_stays_ok(live, fake_net):
    search_console_says(fake_net, "/searchAnalytics/query", REAL_EMPTY)
    data = await core.query_api(HOST, "search_console", "search_analytics", RANGE)
    assert data["status"] == "ok"
    assert data["rows"] == []
    repeat = await core.query_api(HOST, "search_console", "search_analytics", RANGE)
    assert repeat["from_cache"] is True


@pytest.mark.parametrize("body", [REAL_EMPTY, {"rows": []}], ids=json.dumps)
async def test_an_empty_report_stays_ok(live, fake_net, body):
    search_console_says(fake_net, "/searchAnalytics/query", body)
    result = (await core.get_seo_data(HOST, ["search_queries"]))["sources"]["search_queries"]
    assert result["status"] == "ok"
    assert result["rows"] == []


@pytest.mark.parametrize("body", [{"sitemap": []}, {}], ids=json.dumps)
async def test_an_empty_sitemap_list_stays_ok(live, fake_net, body):
    search_console_says(fake_net, "/sitemaps", body)
    result = (await core.get_seo_data(HOST, ["sitemaps"]))["sources"]["sitemaps"]
    assert result["status"] == "ok"
    assert result["reason"] == "Search Console lists no sitemap."
    data = await core.query_api(HOST, "search_console", "sitemaps", {})
    assert data["status"] == "ok"


async def test_a_bad_reply_that_an_old_server_saved_is_not_served(live, fake_net):
    calls = search_console_says(fake_net, "/sitemaps", {"sitemap": []})
    await core.get_seo_data(HOST, ["sitemaps"])
    (saved,) = list(shared.folder().glob("*.json"))
    stored = json.loads(saved.read_text())
    saved.write_text(json.dumps({**stored, "answer": {"message": "try later"}}))
    result = (await core.get_seo_data(HOST, ["sitemaps"]))["sources"]["sitemaps"]
    assert result["status"] == "ok"
    assert result["from_cache"] is False
    assert len(sent(calls, "/sitemaps")) == 2


def test_only_the_named_reads_have_no_shape_check():
    wrong = sorted(unchecked_reads() - NO_SHAPE_CHECK)
    fix = "Pass good= to the read, so that a failed HTTP 200 reply is not an ok empty result."
    assert not wrong, f"{fix} {wrong}"
    stale = sorted(NO_SHAPE_CHECK - unchecked_reads())
    assert not stale, f"Remove these reads from NO_SHAPE_CHECK in {__file__}: {stale}"


def query(api, name, request=None):
    return lambda: core.query_api(HOST, api, name, request)


FEED = f"https://{HOST}/sitemap.xml"
# Each read that PR #21 left with no check. The match finds the request of the read.
OTHER_READS = [
    ("ga4_organic", source("ga4_organic"), ":runReport"),
    ("ga4_landing_pages", source("ga4_landing_pages"), ":runReport"),
    ("run_report", query("ga4", "run_report", {"metrics": [{"name": "sessions"}]}), ":runReport"),
    ("pivot_report", query("ga4", "pivot_report", {}), ":runPivotReport"),
    ("realtime_report", query("ga4", "realtime_report", {}), ":runRealtimeReport"),
    ("metadata", query("ga4", "metadata"), "/metadata"),
    ("crux record", query("crux", "record", {}), ":queryRecord"),
    ("crux history", query("crux", "history", {}), ":queryHistoryRecord"),
    ("sitemap", query("search_console", "sitemap", {"feedpath": FEED}), "sitemap.xml"),
]
OTHER_IDS = [read[0] for read in OTHER_READS]


@pytest.mark.parametrize(("name", "call", "ending"), OTHER_READS, ids=OTHER_IDS)
async def test_a_reply_of_no_documented_field_is_a_failure(live, fake_net, name, call, ending):
    calls = search_console_says(fake_net, ending, {"message": "try later"})
    result = result_of(await call(), name)
    fix = "Pass good= with the documented fields of the reply."
    assert result["status"] == "error", f"{name} showed a failed read as ok. {fix}"
    assert "shape" in result["reason"]
    assert result["rows"] == []
    first = len(sent(calls, ending))
    repeat = result_of(await call(), name)
    assert "did not send it again" in repeat["reason"]
    assert len(sent(calls, ending)) == first


# Saved real empty reports of GA4 held only these fields: no rows, and kind as the docs say.
GA4_EMPTY = {
    "dimensionHeaders": [{"name": "date"}],
    "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
    "totals": [{}],
    "metadata": {"currencyCode": "USD", "timeZone": "Etc/GMT"},
    "kind": "analyticsData#runReport",
}


@pytest.mark.parametrize("body", [{}, {"metadata": {"timeZone": "Etc/GMT"}}], ids=json.dumps)
async def test_a_ga4_report_with_no_kind_is_not_read_as_no_sessions(live, fake_net, body):
    search_console_says(fake_net, ":runReport", body)
    result = (await core.get_seo_data(HOST, ["ga4_organic"]))["sources"]["ga4_organic"]
    assert result["status"] == "error", "A reply with no kind showed no organic sessions."
    assert "shape" in result["reason"]


async def test_a_real_empty_ga4_report_stays_ok(live, fake_net):
    search_console_says(fake_net, ":runReport", GA4_EMPTY)
    result = (await core.get_seo_data(HOST, ["ga4_organic"]))["sources"]["ga4_organic"]
    assert result["status"] == "ok"
    assert result["reason"] == "GA4 reports no organic sessions in the window."
    repeat = (await core.get_seo_data(HOST, ["ga4_organic"]))["sources"]["ga4_organic"]
    assert repeat["from_cache"] is True


# George: a live empty report has responseAggregationType, each GA4 property has standard
# fields, and a live sitemaps.get gave 404 for a sitemap that does not exist, so {} of these reads
# is a failure. Google leaves out an empty sitemap list, so {} of sitemaps.list stays ok.
EMPTY_IS_A_FAILURE = [
    ("search_queries", source("search_queries"), "/searchAnalytics/query"),
    ("search_analytics", search_console("search_analytics", RANGE), "/searchAnalytics/query"),
    ("metadata", query("ga4", "metadata"), "/metadata"),
    ("sitemap", query("search_console", "sitemap", {"feedpath": FEED}), "sitemap.xml"),
]


@pytest.mark.parametrize(
    ("name", "call", "ending"), EMPTY_IS_A_FAILURE, ids=[read[0] for read in EMPTY_IS_A_FAILURE]
)
async def test_an_empty_object_is_not_an_empty_answer(live, fake_net, name, call, ending):
    calls = search_console_says(fake_net, ending, {})
    result = result_of(await call(), name)
    assert result["status"] == "error", f"{name} showed {{}} as an ok empty answer."
    assert "shape" in result["reason"]
    assert result["rows"] == []
    first = len(sent(calls, ending))
    repeat = result_of(await call(), name)
    assert "did not send it again" in repeat["reason"]
    assert len(sent(calls, ending)) == first


# The only checks that pass {}. George: Google leaves out an empty sitemap list.
EMPTY_IS_OK = {("gsc.py", "SITEMAP_LIST")}


def checks_that_pass_empty():
    def owner(tree, call):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.value is call:
                return ast.unparse(node.targets[0])
        return f"line {call.lineno}"

    found = set()
    for path in sorted((SOURCE / "providers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "shared.documented":
                found.add((path.name, owner(tree, node)))
    return found


def test_only_the_named_checks_pass_an_empty_object():
    wrong = sorted(checks_that_pass_empty() - EMPTY_IS_OK)
    fix = "Use shared.has_any or shared.holds, so that {} is a failure, not an ok empty answer."
    assert not wrong, f"{fix} {wrong}"
    stale = sorted(EMPTY_IS_OK - checks_that_pass_empty())
    assert not stale, f"Remove these checks from EMPTY_IS_OK in {__file__}: {stale}"


# A live sitemaps.get of a submitted sitemap gave this record. The dates are placeholders.
SITEMAP_RECORD = {
    "path": FEED,
    "lastSubmitted": "2026-11-01T00:00:00.000Z",
    "isPending": False,
    "isSitemapsIndex": False,
    "type": "sitemap",
    "lastDownloaded": "2026-11-02T00:00:00.000Z",
    "warnings": "0",
    "errors": "0",
    "contents": [{"type": "web", "submitted": "57", "indexed": "0"}],
}


async def test_an_empty_sitemap_record_is_never_saved(live, fake_net):
    search_console_says(fake_net, "sitemap.xml", {})
    data = await core.query_api(HOST, "search_console", "sitemap", {"feedpath": FEED})
    assert data["status"] == "error", "sitemaps.get showed {} as an ok sitemap."
    assert net.BAD_SHAPE in data["reason"]
    assert not [path for path in shared.folder().glob("*.json") if "failed" not in path.name]


async def test_a_sitemap_record_with_a_path_is_ok(live, fake_net):
    search_console_says(fake_net, "sitemap.xml", SITEMAP_RECORD)
    data = await core.query_api(HOST, "search_console", "sitemap", {"feedpath": FEED})
    assert data["status"] == "ok"
    assert data["rows"] == [SITEMAP_RECORD]
