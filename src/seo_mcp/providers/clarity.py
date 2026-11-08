"""Microsoft Clarity live insights, cached for the UTC day."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from seo_mcp import NAME, net, quota, shared, tasks
from seo_mcp.api import Api, Method, Quota, answer, as_rows, one_of, whole
from seo_mcp.config import Site
from seo_mcp.params import BadInput, Params, moved, moved_note, today, too_late
from seo_mcp.results import Refused, Unsupported, ok

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


def project(site: Site) -> str:
    return site.fields.get("clarity_project", site.host)


def _headers(site: Site) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {site.secret('clarity_token')}",
        "Content-Type": "application/json",
        "User-Agent": NAME,
    }


async def _cached(
    site: Site,
    path: Path,
    call: Callable[[dict[str, str], net.Receipt], Awaitable[Any]],
    good: Callable[[Any], bool],
    *,
    daily_429: bool = False,
) -> Any:
    key = project(site)
    quota.show(lambda: quota.view(LEDGER, key))

    async def prepare() -> tuple[dict[str, str], net.Receipt]:
        headers = _headers(site)
        return headers, await quota.spend(LEDGER, key)

    async def send(ready: tuple[dict[str, str], net.Receipt]) -> Any:
        headers, receipt = ready
        try:
            return await call(headers, receipt)
        except net.ApiError as exc:
            if daily_429 and exc.status == 429:
                # A stuck ledger must not hide the 429. The next call gets its own 429.
                with suppress(Refused):
                    await quota.block(LEDGER, key, receipt)
            raise

    return await shared.once(path, send, prepare=prepare, good=good, until=shared.end_of_utc_day)


async def _insights(site: Site, days: int, dimensions: tuple[str, ...]) -> Any:
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
    metrics = await _insights(site, days, ("URL",))
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
SKIP_REASON = (
    'The saved dashboard answer of this UTC day gives a count of 0 for "{question}", so a '
    "sample of these days cannot show such a click. The server did not send it and spent no "
    "call."
)
ZERO = re.compile(r"0+")
TOP = re.compile(r"\btop\b")
MAX_RECORDINGS = 250
DEFAULT_RECORDINGS = 100
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


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
    metrics = await _insights(site, days, _dimensions(request))
    rows = [row for metric in metrics for row in _rows(metric)]
    return answer(rows, more_rows="no", applied=request)


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
    # The end of the UTC day, not the time now: a repeat must be the same request, or it spends.
    latest = f"{today().isoformat()}T23:59:59.999Z"
    if start > latest:
        raise too_late("filters.date start", latest)
    return start, min(end, latest)


def _slash(day: str) -> str:
    return day.replace("-", "/")


def _no_click(row: dict[str, Any], token: str) -> bool:
    """1 column names the click, and it holds exactly 0. Any doubt is not a 0."""
    cells = [value for key, value in row.items() if token in re.sub(r"[^a-z]", "", key.lower())]
    if len(cells) != 1:
        return False
    cell = cells[0]
    return (type(cell) is int and cell == 0) or (
        isinstance(cell, str) and bool(ZERO.fullmatch(cell))
    )


def _is_zero(reply: dict[str, Any], count: tasks.Count, first: str, last: str) -> bool:
    """Clarity rewrites the question, so its words must name the same click and the same days."""
    data, answered = reply.get("data"), reply.get("query")
    if reply.get("dataErrorType") or not data or not isinstance(answered, str):
        return False
    text = answered.lower()
    span = f"between '{_slash(first)} 00:00:00' and '{_slash(last)} 23:59:59'"
    if count.words not in text or span not in text:
        return False
    # A top list sorted by another column can leave out a page with clicks.
    if TOP.search(text) and f"by {count.words}" not in text:
        return False
    token = count.words.replace(" ", "")
    return all(_no_click(row, token) for row in data)


def _zero_count(site: Site, filters: dict[str, Any], start: str, end: str) -> str | None:
    """Give the dashboard question of a saved 0 count for exactly this sample, or None.

    Only the request of a task recipe matches: 1 click flag and whole UTC days. A wrong skip
    hides data, so each doubt sends the sample as before.
    """
    flags = [name for name in filters if name != "date"]
    count = tasks.COUNTS.get(flags[0]) if len(flags) == 1 else None
    if count is None or filters[flags[0]] is not True:
        return None
    if not (start.endswith("T00:00:00.000Z") and end.endswith("T23:59:59.999Z")):
        return None
    first, last = start[:10], end[:10]
    question = tasks.count_query(count.question, _slash(first), _slash(last))
    body = {"query": question, "timezone": "UTC"}
    path = _body_path(project(site), "dashboard", body)
    saved = shared.peek(path, _good_dashboard, shared.end_of_utc_day)
    if saved is None or not _is_zero(saved["answer"], count, first, last):
        return None
    shared.cite(saved)
    return question


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
    asked = filters["date"]["end"]
    shift: dict[str, Any] = {"date_moved": moved(asked, end)} if asked != end else {}
    filters = {**filters, "date": {**filters["date"], "end": end}}
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
    zero = _zero_count(site, filters, start, end)
    if zero:
        quota.show(lambda: quota.view(LEDGER, project(site)))
        result = answer([], more_rows="no", reason=SKIP_REASON.format(question=zero), **shift)
        return {**result, "status": "skipped", "dashboard_question": zero, "applied": body}
    path = _body_path(project(site), "recordings", body)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", RECORDINGS_API, json=body, headers=headers, spent=spent
        )

    rows = as_rows(await _cached(site, path, call, _all_objects))
    reason = moved_note(shift["date_moved"]) if shift else None
    return answer(
        rows, more_rows="possible", reason=reason, sample=SAMPLE_NOTE, applied=body, **shift
    )


async def _dashboard_query(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    text = request["query"]
    if not isinstance(text, str) or not text.strip():
        raise BadInput("query must be text.")
    # Only the outer spaces go: 2 texts with other differences can be 2 questions.
    body = {"query": text.strip(), "timezone": request.get("timezone", "UTC")}
    path = _body_path(project(site), "dashboard", body)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", DASHBOARD_API, json=body, headers=headers, spent=spent
        )

    reply = await _cached(site, path, call, _good_dashboard)
    data = reply.get("data") if isinstance(reply, dict) else None
    rows = as_rows(reply if data is None else data)
    code = reply.get("dataErrorType") if isinstance(reply, dict) else None
    reason = f"Clarity gave data error type {code} for this question." if code else None
    answered = reply.get("query") if isinstance(reply, dict) else None
    return answer(rows, more_rows="no", reason=reason, answered_question=answered, applied=body)


def now(site: Site) -> dict[str, Any]:
    return {"calls": quota.view(LEDGER, project(site))}


DOCS = "https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export-api"
APIS = {
    "clarity": Api(
        title="Microsoft Clarity",
        summary="What visitors do on a page: clicks, scrolls, and lists of recordings.",
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
                "A list of session recordings with links. filters.date is required. After a "
                "saved 0 dashboard count of the same click and whole days, the server skips "
                "it: status skipped, and no call is spent.",
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
