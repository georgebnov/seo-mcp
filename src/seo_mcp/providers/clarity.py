"""Microsoft Clarity live insights, cached for the UTC day."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

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
MISS_REASON = "Clarity has no row for this page, so this is not proof of zero traffic."
LIMIT_REASON = (
    f"The Clarity docs limit an answer to {API_ROWS:,} rows with no paging, and Clarity gave "
    "{rows:,} rows for this request, so it can have more rows. The docs do not say if the limit "
    "counts each metric or the whole answer. The API has no URL or row filter. To read fewer "
    "rows, ask {fewer}."
)
FEWER_INSIGHTS = "fewer dimensions, a dimension with fewer values, or a smaller numOfDays"
FEWER_PAGE_DAYS = "a smaller last_days"
LEDGER = quota.Ledger("clarity-calls", "Clarity project", DAILY_CALLS)


def cache_path(
    project_id: str,
    auth: shared.Identity,
    days: int = WINDOW_DAYS,
    dimensions: tuple[str, ...] = ("URL",),
) -> shared.Keyed:
    key = "-".join(quote(name, safe="") for name in dimensions)
    return shared.named(f"clarity-{quote(project_id, safe='')}-{days}d-{key}", auth=auth)


def _body_path(
    project_id: str, kind: str, body: dict[str, Any], auth: shared.Identity
) -> shared.Keyed:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    return shared.named(f"clarity-{quote(project_id, safe='')}-{kind}-{digest}", auth=auth)


def credential(site: Site) -> tuple[str, shared.Identity]:
    """Read the token 1 time, so that the headers and the cache key name the same token."""
    token = site.secret("clarity_token")
    return token, shared.identity("clarity_token", token)


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


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": NAME,
    }


async def _cached(
    site: Site,
    path: shared.Keyed,
    call: Callable[[dict[str, str], net.Receipt], Awaitable[Any]],
    good: Callable[[Any], bool],
    *,
    token: str,
    daily_429: bool = False,
) -> Any:
    """The ledger key is the project only: a new token reads again, and it spends from 1 count."""
    key = project(site)
    quota.show(lambda: quota.view(LEDGER, key))

    async def prepare() -> tuple[dict[str, str], net.Receipt]:
        headers = _headers(token)
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
    token, auth = credential(site)
    path = cache_path(project(site), auth, days, dimensions)
    return await _cached(site, path, call, _good_insights, token=token, daily_429=True)


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


def _row_limit(metrics: list[dict[str, Any]], fewer: str) -> str | None:
    """Give the reason when the answer can be cut at the row limit, or None.

    The docs do not say if the limit counts each metric or the whole answer. The sum covers both.
    """
    rows = sum(len(metric.get("information") or []) for metric in metrics)
    return LIMIT_REASON.format(rows=rows, fewer=fewer) if rows >= API_ROWS else None


def _page_fields(metrics: list[dict[str, Any]], found: bool) -> dict[str, Any]:
    limit = _row_limit(metrics, FEWER_PAGE_DAYS)
    fields: dict[str, Any] = {"page_match": PAGE_MATCH, "complete": limit is None}
    reason = " ".join(filter(None, (None if found else MISS_REASON, limit)))
    if reason:
        fields["reason"] = reason
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
PROVIDER_OF = {"clarity": "clarity"}

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
TOP_N = re.compile(r"\btop (\d+)\b")
# The endpoint takes only the question text: the Clarity MCP server has no row count field.
TOP_REASON = (
    "Clarity shows only the top {top} rows for this question, and this answer has {top}, so "
    "pages below the top {top} can have counts too. The endpoint has no row count field. To "
    "read 1 page, name its full URL in the question."
)
MAX_RECORDINGS = 250
DEFAULT_RECORDINGS = tasks.SAMPLE_SESSIONS
# A full session row is about 1.8 KB, mostly page speed, titles, and referrers with ad tokens:
# 41 real rows made 68,321 characters, which the tool output of an agent cut. A row keeps only
# what shows which element on which page got the click, and when.
RECORDING_FIELDS: dict[str, Any] = {
    "timestamp": None,
    "link": None,
    "timeline": {"url": None, "timelineEvents": {"eventtype": None, "hash": None, "text": None}},
}
KEPT_FIELDS = (
    "Each row keeps only timestamp, link, and timeline[] with url and timelineEvents[] with "
    "eventtype, hash, and text. url has no query and no fragment. The server drops the other "
    "fields of Clarity."
)
# About 15,000 tokens: an answer of any count stays under the tool output of an agent.
ROWS_BUDGET = 40_000
CUT_REASON = (
    "The answer shows the first {shown} of {fetched} sessions, so that it fits the tool output "
    "of an agent. Ask a smaller count or a narrower filter: another request spends 1 call."
)
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
    reason = _row_limit(metrics, FEWER_INSIGHTS)
    more = "no" if reason is None else "possible"
    return answer(rows, more_rows=more, reason=reason, applied=request)


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


def _zero_count(
    site: Site, filters: dict[str, Any], start: str, end: str, auth: shared.Identity
) -> str | None:
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
    path = _body_path(project(site), "dashboard", body, auth)
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
    token, auth = credential(site)
    zero = _zero_count(site, filters, start, end, auth)
    if zero:
        quota.show(lambda: quota.view(LEDGER, project(site)))
        result = answer([], more_rows="no", reason=SKIP_REASON.format(question=zero), **shift)
        return {**result, "status": "skipped", "dashboard_question": zero, "applied": body}
    path = _body_path(project(site), "recordings", body, auth)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", RECORDINGS_API, json=body, headers=headers, spent=spent
        )

    # The trim comes after the cache, so that a raw answer saved before is trimmed too.
    fetched = [
        _trim(row, RECORDING_FIELDS)
        for row in as_rows(await _cached(site, path, call, _all_objects, token=token))
    ]
    rows = _whole_sessions(fetched)
    notes = [moved_note(shift["date_moved"])] if shift else []
    if len(rows) < len(fetched):
        notes.append(CUT_REASON.format(shown=len(rows), fetched=len(fetched)))
        shift["sessions_fetched"] = len(fetched)
    return answer(
        rows,
        more_rows="possible",
        reason=" ".join(notes) or None,
        sample=SAMPLE_NOTE,
        kept_fields=KEPT_FIELDS,
        applied=body,
        **shift,
    )


def _bare_url(url: Any) -> Any:
    """Drop the query, the fragment, and a login: a query can hold an ad token, such as fbclid."""
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    host = f"{parts.hostname or ''}:{port}" if port else parts.hostname or ""
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _trim(row: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only the listed fields. A field of another shape goes, so no nested data passes."""
    kept: dict[str, Any] = {}
    for name, inner in fields.items():
        if name not in row:
            continue
        value = row[name]
        if inner is None and not isinstance(value, dict | list):
            kept[name] = _bare_url(value) if name == "url" else value
        elif inner is not None and _all_objects(value):
            kept[name] = [_trim(item, inner) for item in value]
    return kept


def _whole_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cut at a whole session, and keep the first one always, so that a sample is never empty."""
    kept: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        used += len(json.dumps(row, default=str)) + 2
        if kept and used > ROWS_BUDGET:
            break
        kept.append(row)
    return kept


async def _dashboard_query(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    text = request["query"]
    if not isinstance(text, str) or not text.strip():
        raise BadInput("query must be text.")
    # Only the outer spaces go: 2 texts with other differences can be 2 questions.
    body = {"query": text.strip(), "timezone": request.get("timezone", "UTC")}
    token, auth = credential(site)
    path = _body_path(project(site), "dashboard", body, auth)

    async def call(headers: dict[str, str], spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", DASHBOARD_API, json=body, headers=headers, spent=spent
        )

    reply = await _cached(site, path, call, _good_dashboard, token=token)
    data = reply.get("data") if isinstance(reply, dict) else None
    rows = as_rows(reply if data is None else data)
    code = reply.get("dataErrorType") if isinstance(reply, dict) else None
    reason = f"Clarity gave data error type {code} for this question." if code else None
    answered = reply.get("query") if isinstance(reply, dict) else None
    top = _top(answered)
    more = "possible" if top is not None and len(rows) >= top else "no"
    if more == "possible":
        reason = " ".join(filter(None, (reason, TOP_REASON.format(top=top))))
    return answer(rows, more_rows=more, reason=reason, answered_question=answered, applied=body)


def _top(answered: Any) -> int | None:
    """Give the smallest N of a "top N" list in the question that Clarity rewrote, or None."""
    if not isinstance(answered, str):
        return None
    sizes = [int(size) for size in TOP_N.findall(answered.lower())]
    return min(sizes) if sizes else None


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
                "A list of session recordings with links, newest first. filters.date is "
                f"required, and count is {DEFAULT_RECORDINGS} by default. Each row keeps only "
                "the time, the link, and each page with its events. After a saved 0 dashboard "
                "count of the same click and whole days, the server skips it: status skipped, "
                "and no call is spent.",
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
            "Recordings older than 30 days and dashboard data older than 9 months: Clarity "
            "deletes them. recordings and dashboard_query refuse no time frame, so rows of old "
            "days are simply absent. Empty rows do not prove zero activity.",
        ),
    )
}
