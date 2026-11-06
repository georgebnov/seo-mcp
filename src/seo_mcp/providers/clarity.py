"""Microsoft Clarity live insights, cached for the UTC day."""

import asyncio
import fcntl
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from seo_mcp import NAME, net, quota
from seo_mcp.api import Api, Method, Quota, answer, as_rows, one_of, whole
from seo_mcp.config import Site
from seo_mcp.params import BadInput, Params
from seo_mcp.results import Unsupported, ok

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
# The official Clarity MCP server calls these, with the same token. Both read.
RECORDINGS_API = "https://clarity.microsoft.com/mcp/recordings/sample"
DASHBOARD_API = "https://clarity.microsoft.com/mcp/dashboard/query"
ENDPOINTS = (
    net.Endpoint(
        "GET", "www.clarity.ms", r"/export-data/api/v1/project-live-insights", "clarity-calls"
    ),
    net.Endpoint("POST", "clarity.microsoft.com", r"/mcp/recordings/sample", "clarity-calls"),
    net.Endpoint("POST", "clarity.microsoft.com", r"/mcp/dashboard/query", "clarity-calls"),
)
# The API allows 10 calls for each project each day, for the last 1 to 3 days.
WINDOW_DAYS = 3
THIN_SESSIONS = 100
ROWS_PER_METRIC = 5
DAILY_CALLS = 10
# The API cuts an answer at this many rows and has no paging.
API_ROWS = 1000
PAGE_MATCH = "The API has no URL filter. The server keeps the URL rows of this page, any scheme."
MISS_REASON = (
    f"Clarity has no row for this page. An answer has at most {API_ROWS} rows for each metric, "
    "so this is not proof of zero traffic."
)
LOCK_POLL_SECONDS = 0.05
# A failed call can count at the API, so the same request waits this long. This stops a burst
# of agents. It does not protect the day: a long outage still spends 1 call in each window.
FAILURE_WINDOW_SECONDS = 60
LEDGER = quota.Ledger("clarity-calls", "Clarity project", DAILY_CALLS)


def cache_path(
    project_id: str, days: int = WINDOW_DAYS, dimensions: tuple[str, ...] = ("URL",)
) -> Path:
    key = "-".join(quote(name, safe="") for name in dimensions)
    return quota.folder() / f"clarity-{quote(project_id, safe='')}-{days}d-{key}.json"


def _body_path(project_id: str, kind: str, body: dict[str, Any]) -> Path:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    return quota.folder() / f"clarity-{quote(project_id, safe='')}-{kind}-{digest}.json"


def ledger_path(project_id: str) -> Path:
    return quota.path(LEDGER, project_id)


def _read_cache(path: Path, good: Callable[[Any], bool]) -> dict[str, Any] | None:
    try:
        stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        same_day = datetime.fromisoformat(stored["cached_at"]).date() == datetime.now(UTC).date()
        usable = same_day and good(stored["answer"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return stored if usable else None


def _all_objects(items: Any) -> bool:
    return isinstance(items, list) and all(isinstance(item, dict) for item in items)


def _good_insights(reply: Any) -> bool:
    return _all_objects(reply) and all(
        _all_objects(metric.get("information") or []) for metric in reply
    )


def _good_dashboard(reply: Any) -> bool:
    if not isinstance(reply, dict):
        return False
    data = reply.get("data")
    return _all_objects(data) or (data is None and bool(reply.get("dataErrorType")))


def failure_path(path: Path) -> Path:
    return path.with_suffix(".failed.json")


def _mark_failure(path: Path, status: int | None, detail: str | None = None) -> None:
    stored = {"failed_at": datetime.now(UTC).isoformat(), "status": status, "detail": detail}
    quota.write_json(failure_path(path), stored)


def _recent_failure(path: Path) -> net.ApiError | None:
    try:
        stored = json.loads(failure_path(path).read_text(encoding="utf-8"))
        status = stored["status"]
        age = (datetime.now(UTC) - datetime.fromisoformat(stored["failed_at"])).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 0 <= age < FAILURE_WINDOW_SECONDS or not (status is None or type(status) is int):
        return None
    left = math.ceil(FAILURE_WINDOW_SECONDS - age)
    return net.ApiError(
        status,
        net.BAD_SHAPE if stored.get("detail") == net.BAD_SHAPE else None,
        note=f"The same request failed {int(age)} seconds ago, so the server did not send it "
        f"again and spent no call. It can send it after {left} seconds.",
    )


def project(site: Site) -> str:
    return site.fields.get("clarity_project", site.host)


def _headers(site: Site) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {site.secret('clarity_token')}",
        "Content-Type": "application/json",
        "User-Agent": NAME,
    }


@asynccontextmanager
async def _only_fetcher(path: Path) -> AsyncIterator[None]:
    """Let 1 caller fetch an answer, in this process or another, while the others wait.

    Parallel agents run separate server processes on 1 cache folder, and each miss spends a
    call. Each open file has its own flock, so this also separates 2 runs in 1 process. The
    source timeout ends a wait, and the kernel frees the lock when a process dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(LOCK_POLL_SECONDS)
        yield


async def _cached(
    site: Site,
    path: Path,
    call: Callable[[dict[str, str], net.Receipt], Awaitable[Any]],
    good: Callable[[Any], bool],
    *,
    daily_429: bool = False,
) -> tuple[dict[str, Any], bool]:
    key = project(site)
    quota.show(lambda: quota.view(LEDGER, key))
    async with _only_fetcher(path):
        stored = _read_cache(path, good)
        if stored is not None:
            return stored, True
        failure = _recent_failure(path)
        if failure:
            raise failure
        headers = _headers(site)
        receipt = quota.spend(LEDGER, key)
        try:
            reply = await call(headers, receipt)
            if not good(reply):
                raise net.ApiError(200, net.BAD_SHAPE)
        except net.ApiError as exc:
            if daily_429 and exc.status == 429:
                quota.block(LEDGER, key, receipt)
            if exc.sent:
                _mark_failure(path, exc.status, exc.detail)
            raise
        except asyncio.CancelledError:
            # The source timeout ended a call in flight, and the API can have counted it.
            _mark_failure(path, None)
            raise
        stored = {"cached_at": datetime.now(UTC).isoformat(), "answer": reply}
        quota.write_json(path, stored)
        return stored, False


async def _insights(
    site: Site, days: int, dimensions: tuple[str, ...]
) -> tuple[dict[str, Any], bool]:
    params: dict[str, Any] = {"numOfDays": days}
    params |= {f"dimension{at}": name for at, name in enumerate(dimensions, 1)}

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(ENDPOINTS, "GET", API, params=params, headers=headers, spent=spent)

    # Only this API documents 429 as the daily limit. The /mcp endpoints have no reference.
    path = cache_path(project(site), days, dimensions)
    return await _cached(site, path, call, _good_insights, daily_429=True)


def _number(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _page_key(url: str) -> tuple[str | None, str, str]:
    parts = urlsplit(url)
    return parts.hostname, parts.path or "/", parts.query


def _only_page(metrics: list[dict[str, Any]], page: str) -> list[dict[str, Any]]:
    wanted = _page_key(page)
    return [
        {
            **metric,
            "information": [
                item
                for item in metric.get("information") or []
                if _page_key(str(item.get("Url", ""))) == wanted
            ],
        }
        for metric in metrics
    ]


def _rows(metric: dict[str, Any]) -> list[dict[str, Any]]:
    items = metric.get("information") or []
    return [{"metric": metric.get("metricName"), **item} for item in items]


def _page_fields(metrics: list[dict[str, Any]], found: bool) -> dict[str, Any]:
    capped = any(len(metric.get("information") or []) >= API_ROWS for metric in metrics)
    fields: dict[str, Any] = {"page_match": PAGE_MATCH, "complete": not capped}
    if not found:
        fields["reason"] = MISS_REASON
    return fields


async def clarity(site: Site, params: Params) -> dict[str, Any]:
    days = params.last_days or WINDOW_DAYS
    if days > WINDOW_DAYS:
        raise Unsupported(["last_days"], "Clarity gives only the last 1, 2, or 3 days.")
    stored, from_cache = await _insights(site, days, ("URL",))
    metrics = stored["answer"]
    shown = _only_page(metrics, params.page) if params.page else metrics
    traffic = [row for m in shown if m.get("metricName") == "Traffic" for row in _rows(m)]
    rows = [row for metric in shown for row in _rows(metric)[:ROWS_PER_METRIC]]
    found = bool(traffic) or not params.page
    sessions = int(sum(_number(row.get("totalSessionCount")) for row in traffic))
    extra = _page_fields(metrics, bool(rows)) if params.page else {}
    return ok(
        rows,
        thin=sessions < THIN_SESSIONS,
        limit=len(rows),
        window={"last_days": days},
        totals={"sessions": sessions if found else None},
        from_cache=from_cache,
        cached_at=stored["cached_at"],
        **extra,
    )


SOURCES = {"clarity": clarity}
ACCEPTS = {"clarity": frozenset({"last_days", "page"})}

DIMENSIONS = (
    "Browser",
    "Device",
    "Country/Region",
    "OS",
    "Source",
    "Medium",
    "Campaign",
    "Channel",
    "URL",
)
SORTS = (
    "SessionStart_DESC",
    "SessionStart_ASC",
    "SessionDuration_ASC",
    "SessionDuration_DESC",
    "SessionClickCount_ASC",
    "SessionClickCount_DESC",
    "PageCount_ASC",
    "PageCount_DESC",
)
# The filter names, values, and units come from the official Clarity MCP server. See
# THIRD_PARTY_NOTICES.md. The API has no reference page, so its schema is the only record.
ONE_VALUE_FILTERS = {
    "userType": ("NewUser", "ReturningUser"),
    "sessionIntent": ("Low Intention", "Medium Intention", "High Intention"),
}
LIST_VALUE_FILTERS = {
    "deviceType": ("Mobile", "Tablet", "PC", "Email", "Other"),
    "browser": (
        "Bot",
        "MiuiBrowser",
        "Chrome",
        "CoralWebView",
        "Edge",
        "Other",
        "Firefox",
        "IE",
        "Unknown",
        "Headless",
        "MobileApp",
        "Opera",
        "OperaMini",
        "Safari",
        "Samsung",
        "SamsungInternet",
        "Sogou",
        "UCBrowser",
        "YandexBrowser",
        "QQBrowser",
    ),
    "os": (
        "BlackBerry",
        "Android",
        "ChromeOS",
        "iOS",
        "Linux",
        "MacOS",
        "Other",
        "Windows",
        "WindowsMobile",
    ),
    "channel": (
        "OrganicSearch",
        "Direct",
        "Email",
        "Display",
        "Social",
        "PaidSearch",
        "Other",
        "Affiliate",
        "Referral",
        "Video",
        "Audio",
        "SMS",
        "AITools",
        "PaidAITools",
    ),
}
URL_OPERATORS = (
    "contains",
    "startsWith",
    "endsWith",
    "excludes",
    "isExactly",
    "isExactlyNot",
    "matchesRegex",
    "excludesRegex",
)
URL_FILTERS = frozenset({"visitedUrls", "entryUrls", "exitUrls"})
TEXT_FILTERS = frozenset({"referringUrl", "clickedText", "productName"})
TEXT_LIST_FILTERS = frozenset(
    {
        "country",
        "city",
        "state",
        "source",
        "medium",
        "campaign",
        "smartEvents",
        "javascriptErrors",
        "clickErrors",
        "productBrand",
        "checkoutAbandonmentStep",
    }
)
FLAG_FILTERS = frozenset(
    {
        "enteredTextPresent",
        "selectedTextPresent",
        "resizeEventPresent",
        "cursorMovement",
        "deadClickPresent",
        "rageClickPresent",
        "excessiveScrollPresent",
        "quickbackClickPresent",
        "productPurchases",
        "productAvailability",
    }
)
# A range filter has min and max. Each is a number or null. A value here is the valid span.
RANGE_FILTERS: dict[str, tuple[float, float] | None] = {
    "visiblePageDuration": None,
    "hiddenPageDuration": None,
    "pageDuration": None,
    "sessionDuration": None,
    "scrollDepth": (0, 100),
    "pagesCount": None,
    "pageClickEventCount": None,
    "sessionClickEventCount": None,
    "performanceScore": (0, 100),
    "largestContentfulPaint": None,
    "cumulativeLayoutShift": None,
    "firstInputDelay": None,
    "productRating": None,
    "productRatingsCount": None,
    "productPrice": None,
}
RECORDING_FILTERS = frozenset(
    {"date", *ONE_VALUE_FILTERS, *LIST_VALUE_FILTERS, *RANGE_FILTERS}
    | URL_FILTERS
    | TEXT_FILTERS
    | TEXT_LIST_FILTERS
    | FLAG_FILTERS
)
RECORDING_FILTER_UNITS = {
    "visiblePageDuration": "minutes",
    "hiddenPageDuration": "minutes",
    "pageDuration": "minutes",
    "sessionDuration": "minutes",
    "scrollDepth": "percent, 0 to 100",
    "performanceScore": "score, 0 to 100",
    "largestContentfulPaint": "seconds",
    "firstInputDelay": "milliseconds",
}
# Examples only: a site can define its own smart events, so the server does not limit them.
SMART_EVENT_EXAMPLES = ("Purchase", "ContactUs", "SignUp", "BeginCheckout", "Download", "Login")
DASHBOARD_RULES = (
    "Ask for 1 metric or 1 trend in each question.",
    "Name the time frame, the full URL, and the segment in the text.",
    "Example: Dead click count per page between 2026/09/25 and 2026/10/23.",
    "answered_question shows the question that Clarity rewrote and answered: read its dates.",
    "Column names come from the text of the question, so they can change.",
)
SAMPLE_NOTE = (
    "The API gives a sample of sessions, not all of them, so do not use these rows as a total. "
    "Use dashboard_query for a count."
)
MAX_RECORDINGS = 250
DEFAULT_RECORDINGS = 100
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _stored_fields(stored: dict[str, Any], hit: bool) -> dict[str, Any]:
    return {"from_cache": hit, "cached_at": stored["cached_at"]}


def _dimensions(request: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for at in (1, 2, 3):
        field = f"dimension{at}"
        if field in request:
            if len(names) != at - 1:
                raise BadInput(f"Fill dimension1 up to {field} with no gap.")
            one_of(field, request[field], DIMENSIONS)
            names.append(request[field])
    return tuple(names)


async def _live_insights(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    days = whole("numOfDays", request["numOfDays"], 1, WINDOW_DAYS)
    stored, hit = await _insights(site, days, _dimensions(request))
    rows = [row for metric in stored["answer"] for row in _rows(metric)]
    return answer(rows, more_rows="no", applied=request, **_stored_fields(stored, hit))


def _check_date(filters: dict[str, Any]) -> tuple[str, str]:
    span = filters.get("date")
    start = span.get("start") if isinstance(span, dict) else None
    end = span.get("end") if isinstance(span, dict) else None
    shape = "filters.date needs start and end in the form 2026-01-31T00:00:00.000Z."
    if not isinstance(start, str) or not isinstance(end, str):
        raise BadInput(shape)
    if not INSTANT.fullmatch(start) or not INSTANT.fullmatch(end):
        raise BadInput(shape)
    if start > end:
        raise BadInput("filters.date start must not be after end.")
    return start, end


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_urls(name: str, value: Any) -> None:
    shape = f"filters.{name} must be a list of objects with url and operator."
    if not isinstance(value, list):
        raise BadInput(shape)
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise BadInput(shape)
        one_of(f"filters.{name} operator", item.get("operator"), URL_OPERATORS)


def _is_bound(value: Any, span: tuple[float, float] | None) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return span is None or span[0] <= value <= span[1]


def _check_range(name: str, value: Any) -> None:
    span = RANGE_FILTERS[name]
    valid = (
        isinstance(value, dict)
        and set(value) == {"min", "max"}
        and all(_is_bound(bound, span) for bound in value.values())
    )
    if not valid:
        limit = f" from {span[0]} to {span[1]}" if span else ""
        raise BadInput(f"filters.{name} needs min and max: each a number{limit} or null.")


def _check_filter(name: str, value: Any) -> None:
    if name in ONE_VALUE_FILTERS:
        one_of(f"filters.{name}", value, ONE_VALUE_FILTERS[name])
    elif name in LIST_VALUE_FILTERS:
        if not isinstance(value, list):
            raise BadInput(f"filters.{name} must be a list.")
        for item in value:
            one_of(f"filters.{name}", item, LIST_VALUE_FILTERS[name])
    elif name in URL_FILTERS:
        _check_urls(name, value)
    elif name in TEXT_LIST_FILTERS:
        if not _is_text_list(value):
            raise BadInput(f"filters.{name} must be a list of text.")
    elif name in TEXT_FILTERS:
        if not isinstance(value, str):
            raise BadInput(f"filters.{name} must be text.")
    elif name in FLAG_FILTERS:
        if not isinstance(value, bool):
            raise BadInput(f"filters.{name} must be true or false.")
    elif name in RANGE_FILTERS:
        _check_range(name, value)


async def _recordings(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    filters = request["filters"]
    if not isinstance(filters, dict):
        raise BadInput("filters must be an object.")
    unknown = sorted(set(filters) - RECORDING_FILTERS)
    if unknown:
        raise BadInput(f"filters has no field {', '.join(unknown)}.")
    start, end = _check_date(filters)
    for name, value in filters.items():
        _check_filter(name, value)
    sort = request.get("sortBy", SORTS[0])
    one_of("sortBy", sort, SORTS)
    count = whole("count", request.get("count", DEFAULT_RECORDINGS), 1, MAX_RECORDINGS)
    body = {
        "sortBy": SORTS.index(sort),
        "start": start,
        "end": end,
        "filters": filters,
        "count": count,
    }
    path = _body_path(project(site), "recordings", body)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", RECORDINGS_API, json=body, headers=headers, spent=spent
        )

    stored, hit = await _cached(site, path, call, _all_objects)
    rows = as_rows(stored["answer"])
    fields = _stored_fields(stored, hit)
    return answer(rows, more_rows="possible", sample=SAMPLE_NOTE, applied=body, **fields)


async def _dashboard_query(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    text = request["query"]
    if not isinstance(text, str) or not text.strip():
        raise BadInput("query must be text.")
    body = {"query": text, "timezone": request.get("timezone", "UTC")}
    path = _body_path(project(site), "dashboard", body)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", DASHBOARD_API, json=body, headers=headers, spent=spent
        )

    stored, hit = await _cached(site, path, call, _good_dashboard)
    reply = stored["answer"]
    data = reply.get("data") if isinstance(reply, dict) else None
    rows = as_rows(reply if data is None else data)
    code = reply.get("dataErrorType") if isinstance(reply, dict) else None
    reason = f"Clarity gave data error type {code} for this question." if code else None
    answered = reply.get("query") if isinstance(reply, dict) else None
    fields = _stored_fields(stored, hit)
    return answer(
        rows, more_rows="no", reason=reason, answered_question=answered, applied=body, **fields
    )


def now(site: Site) -> dict[str, Any]:
    return {"calls": quota.view(LEDGER, project(site))}


DOCS = "https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export-api"
APIS = {
    "clarity": Api(
        title="Microsoft Clarity",
        docs=DOCS,
        quota=Quota(
            DOCS,
            (
                f"{DAILY_CALLS} API requests for each project each day. HTTP 429 means that "
                "the daily limit is passed.",
                "The docs state no reset time. The server assumes 00:00 UTC.",
                "The /mcp endpoints have no reference page, so the docs do not say if they "
                f"share the {DAILY_CALLS}. The server counts them in the same {DAILY_CALLS}.",
            ),
            frozenset({"clarity_token"}),
            now,
        ),
        methods={
            "live_insights": Method(
                _live_insights,
                "The Data Export API: metrics of the last 1 to 3 days by up to 3 dimensions.",
                frozenset({"numOfDays", "dimension1", "dimension2", "dimension3"}),
                frozenset({"numOfDays"}),
            ),
            "recordings": Method(
                _recordings,
                "A list of session recordings with links. filters.date is required.",
                frozenset({"filters", "sortBy", "count"}),
                frozenset({"filters"}),
            ),
            "dashboard_query": Method(
                _dashboard_query,
                "1 plain-language question about dashboard data, with a time frame in the text.",
                frozenset({"query", "timezone"}),
                frozenset({"query"}),
            ),
        },
        options={
            "dimensions": list(DIMENSIONS),
            "num_of_days": [1, 2, 3],
            "sort_by": list(SORTS),
            "recording_filters": sorted(RECORDING_FILTERS),
            "recording_filter_values": ONE_VALUE_FILTERS | LIST_VALUE_FILTERS,
            "recording_filter_units": RECORDING_FILTER_UNITS,
            "url_filters": "Each of visitedUrls, entryUrls, and exitUrls is a list of objects "
            "with url and operator.",
            "url_operators": list(URL_OPERATORS),
            "range_filters": "Each has min and max: a number or null.",
            "smart_event_examples": list(SMART_EVENT_EXAMPLES),
            "dashboard_rules": list(DASHBOARD_RULES),
            "max_recordings": MAX_RECORDINGS,
            "calls_per_project_per_day": DAILY_CALLS,
            "budget": "Each live call of each method spends 1 of the daily calls. "
            "The same request on the same UTC day comes from the cache and spends none.",
        },
        gaps=(
            "Heatmaps: the CSV and PNG download is a button in the Clarity web UI. "
            "No API or MCP endpoint gives heatmap data.",
            "Recording playback and download: the recordings method lists sessions with links, "
            "and Clarity has no recording download.",
            "live_insights data older than 3 days.",
            "More than 1,000 rows in live_insights: the API has no paging.",
            "A retention limit: recordings and dashboard_query refuse no time frame, and rows "
            "of old days are simply absent. Empty rows do not prove zero activity.",
        ),
    )
}
