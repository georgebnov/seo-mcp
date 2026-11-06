"""Microsoft Clarity live insights, cached for the UTC day."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from seo_mcp import NAME, net
from seo_mcp.api import Api, Method, answer, as_rows, one_of, whole
from seo_mcp.config import Site
from seo_mcp.params import BadInput, Params
from seo_mcp.results import Refused, Unsupported, ok

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
# The official Clarity MCP server calls these 2, with the same token. Both read.
RECORDINGS_API = "https://clarity.microsoft.com/mcp/recordings/sample"
DASHBOARD_API = "https://clarity.microsoft.com/mcp/dashboard/query"
ENDPOINTS = (
    net.Endpoint("GET", "www.clarity.ms", r"/export-data/api/v1/project-live-insights"),
    net.Endpoint("POST", "clarity.microsoft.com", r"/mcp/recordings/sample"),
    net.Endpoint("POST", "clarity.microsoft.com", r"/mcp/dashboard/query"),
)
CACHE_ENV = NAME.upper().replace("-", "_") + "_CACHE_DIR"
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
BROKEN_LEDGER = "The Clarity call ledger is broken, so the server spends no call. Delete the file."
LOCK_POLL_SECONDS = 0.05


def _folder() -> Path:
    return Path(os.environ.get(CACHE_ENV) or f"~/.cache/{NAME}").expanduser()


def cache_path(host: str, days: int = WINDOW_DAYS, dimensions: tuple[str, ...] = ("URL",)) -> Path:
    key = "-".join(quote(name, safe="") for name in dimensions)
    return _folder() / f"clarity-{quote(host, safe='')}-{days}d-{key}.json"


def _body_path(host: str, kind: str, body: dict[str, Any]) -> Path:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    return _folder() / f"clarity-{quote(host, safe='')}-{kind}-{digest}.json"


def ledger_path(project: str) -> Path:
    return _folder() / f"clarity-calls-{quote(project, safe='')}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        same_day = datetime.fromisoformat(stored["cached_at"]).date() == datetime.now(UTC).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return stored if same_day else None


def _write_cache(path: Path, stored: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, name = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(stored, file)
    Path(name).replace(path)


def _used_today(path: Path, today: str) -> int:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return int(stored["used"]) if stored["day"] == today else 0
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        # A broken count must not read as 0: that would spend the quota without control.
        raise Refused(BROKEN_LEDGER) from None


def _spend(project: str) -> int:
    """Count 1 call before it goes out. A failed call counts: the API can still count it."""
    path = ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    today = datetime.now(UTC).date().isoformat()
    # Each agent session runs its own server process, so a lock inside 1 process is not enough.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        used = _used_today(path, today)
        if used >= DAILY_CALLS:
            raise Refused(
                f"This server has spent the {DAILY_CALLS} calls of this Clarity project for "
                "this UTC day. Use a cached answer, or call again after 00:00 UTC."
            )
        _write_cache(path, {"day": today, "used": used + 1})
    return used + 1


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
    site: Site, path: Path, call: Callable[[dict[str, str]], Awaitable[Any]]
) -> tuple[dict[str, Any], bool]:
    async with _only_fetcher(path):
        stored = _read_cache(path)
        if stored is not None:
            return stored, True
        headers = _headers(site)
        _spend(project(site))
        stored = {"cached_at": datetime.now(UTC).isoformat(), "answer": await call(headers)}
        _write_cache(path, stored)
        return stored, False


async def _insights(
    site: Site, days: int, dimensions: tuple[str, ...]
) -> tuple[dict[str, Any], bool]:
    params: dict[str, Any] = {"numOfDays": days}
    params |= {f"dimension{at}": name for at, name in enumerate(dimensions, 1)}

    async def call(headers: dict[str, str]) -> Any:
        return await net.request(ENDPOINTS, "GET", API, params=params, headers=headers)

    return await _cached(site, cache_path(site.host, days, dimensions), call)


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
RECORDING_FILTERS = frozenset(
    {
        "date",
        "referringUrl",
        "userType",
        "sessionIntent",
        "visitedUrls",
        "entryUrls",
        "exitUrls",
        "country",
        "city",
        "state",
        "deviceType",
        "browser",
        "os",
        "source",
        "medium",
        "campaign",
        "channel",
        "smartEvents",
        "javascriptErrors",
        "clickErrors",
        "clickedText",
        "enteredTextPresent",
        "selectedTextPresent",
        "resizeEventPresent",
        "cursorMovement",
        "deadClickPresent",
        "rageClickPresent",
        "excessiveScrollPresent",
        "quickbackClickPresent",
        "visiblePageDuration",
        "hiddenPageDuration",
        "pageDuration",
        "sessionDuration",
        "scrollDepth",
        "pagesCount",
        "pageClickEventCount",
        "sessionClickEventCount",
        "performanceScore",
        "largestContentfulPaint",
        "cumulativeLayoutShift",
        "firstInputDelay",
        "productRating",
        "productRatingsCount",
        "productPrice",
        "productName",
        "productPurchases",
        "productAvailability",
        "productBrand",
        "checkoutAbandonmentStep",
    }
)
MAX_RECORDINGS = 250
DEFAULT_RECORDINGS = 100
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _calls_used(site: Site) -> int:
    today = datetime.now(UTC).date().isoformat()
    return _used_today(ledger_path(project(site)), today)


def _stored_fields(site: Site, stored: dict[str, Any], hit: bool) -> dict[str, Any]:
    return {
        "from_cache": hit,
        "cached_at": stored["cached_at"],
        "calls_used_today": _calls_used(site),
    }


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
    return answer(rows, more_rows="no", applied=request, **_stored_fields(site, stored, hit))


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


async def _recordings(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    filters = request["filters"]
    if not isinstance(filters, dict):
        raise BadInput("filters must be an object.")
    unknown = sorted(set(filters) - RECORDING_FILTERS)
    if unknown:
        raise BadInput(f"filters has no field {', '.join(unknown)}.")
    start, end = _check_date(filters)
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
    path = _body_path(site.host, "recordings", body)

    async def call(headers: dict[str, str]) -> Any:
        return await net.request(ENDPOINTS, "POST", RECORDINGS_API, json=body, headers=headers)

    stored, hit = await _cached(site, path, call)
    rows = as_rows(stored["answer"])
    more = "possible" if len(rows) >= count else "no"
    return answer(rows, more_rows=more, applied=body, **_stored_fields(site, stored, hit))


async def _dashboard_query(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    text = request["query"]
    if not isinstance(text, str) or not text.strip():
        raise BadInput("query must be text.")
    body = {"query": text, "timezone": request.get("timezone", "UTC")}
    path = _body_path(site.host, "dashboard", body)

    async def call(headers: dict[str, str]) -> Any:
        return await net.request(ENDPOINTS, "POST", DASHBOARD_API, json=body, headers=headers)

    stored, hit = await _cached(site, path, call)
    reply = stored["answer"]
    data = reply.get("data") if isinstance(reply, dict) else None
    rows = as_rows(reply if data is None else data)
    code = reply.get("dataErrorType") if isinstance(reply, dict) else None
    reason = f"Clarity gave data error type {code} for this question." if code else None
    return answer(
        rows, more_rows="no", reason=reason, applied=body, **_stored_fields(site, stored, hit)
    )


APIS = {
    "clarity": Api(
        title="Microsoft Clarity",
        docs="https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export-api",
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
        ),
    )
}
