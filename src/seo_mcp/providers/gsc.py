"""Google Search Console: queries, pages, quick wins, sitemaps, and index status."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

from seo_mcp import net, quota, shared
from seo_mcp.api import Api, Method, Quota, answer, one_of, row_limit, whole
from seo_mcp.config import Site
from seo_mcp.params import (
    DATES,
    MAX_TEXT,
    NONE,
    SLACK_DAYS,
    BadInput,
    Params,
    moved,
    moved_note,
    page_url,
    period,
    today,
    too_late,
)
from seo_mcp.providers import _google
from seo_mcp.results import Refused, ok

Rows = list[dict[str, Any]]

HOST = "searchconsole.googleapis.com"
SITES_API = f"https://{HOST}/webmasters/v3/sites"
INSPECT_API = f"https://{HOST}/v1/urlInspection/index:inspect"
ENDPOINTS = (
    net.Endpoint("POST", HOST, r"/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    net.Endpoint("GET", HOST, r"/webmasters/v3/sites/[^/]+/sitemaps"),
    net.Endpoint("GET", HOST, r"/webmasters/v3/sites/[^/]+/sitemaps/[^/]+"),
    net.Endpoint("POST", HOST, r"/v1/urlInspection/index:inspect", "url-inspection-calls"),
)
WINDOW_DAYS = 28
# The docs say that data is typically available after 2-3 days. A default range ends on the
# last complete day that the API gives, and on this estimate only when the API gives none.
LAG_DAYS = 3
# The docs give the dates of a request "in PT time (UTC - 7:00/8:00)". The server reads no zone:
# a PT day is never after the UTC date, so the UTC date is the newest day that can have data.
ZONE = "PT (UTC-7 or UTC-8)"
# The docs: a query grouped by date for the past 10 days finds the newest data.
COVERAGE_DAYS = 10
API_HOW = "metadata.firstIncompleteDate of Search Console, minus 1 day."
ESTIMATE_HOW = (
    f"An estimate: {LAG_DAYS + SLACK_DAYS} days before the UTC date of the answer. The "
    f"docs say that data takes 2-3 days, and a {ZONE} day can be 1 day before the UTC date."
)
ASK_HOW = f"{ESTIMATE_HOW} Send dataState all with the dimension date for the value of the API."
# The API maximum for 1 call. A sort of fewer rows can miss the real top rows.
FETCH_ROWS = 25000
THIN_IMPRESSIONS = 100
THIN_INSPECTED_URLS = 3
INSPECT_URLS = 5
QUICK_WIN_POSITIONS = (4, 10)
HISTORY_MONTHS = 16
LEDGER = quota.Ledger("url-inspection-calls", "Search Console property", 2000)
MISS_REASON = (
    "No rows match. A page or query filter is an exact, case-sensitive match, and "
    "Search Console hides rare queries."
)
# A live read of a date range with no data gave only responseAggregationType, with no rows.
REPORT = shared.documented("rows", "responseAggregationType", "metadata")
SITEMAP_LIST = shared.documented("sitemap")
SITEMAP = shared.documented(
    "path",
    "lastSubmitted",
    "isPending",
    "isSitemapsIndex",
    "type",
    "lastDownloaded",
    "warnings",
    "errors",
    "contents",
)


Known = tuple[date, str, str]


def _estimate(day: date) -> Known:
    return day - timedelta(days=LAG_DAYS + SLACK_DAYS), "estimate", ESTIMATE_HOW


def _newest_end(name: str, start: date, end: date) -> date:
    """The UTC date is the newest day: dataState all gives it, and it is partial.

    In PT it can still be the day before. That day then has no data, and it is marked partial:
    a valid day never moves.
    """
    latest = today()
    if start > latest:
        raise too_late(name, latest)
    return min(end, latest)


def _named(asked: Params) -> dict[str, str] | None:
    """The range that the agent named. A start past the newest day is refused before a call."""
    if not (asked.start and asked.end):
        return None
    end = _newest_end("start_date", asked.start, asked.end)
    return {"start": asked.start.isoformat(), "end": end.isoformat()}


def _default(asked: Params, last: date) -> dict[str, str]:
    start = last - timedelta(days=(asked.last_days or WINDOW_DAYS) - 1)
    return {"start": start.isoformat(), "end": last.isoformat()}


def _oldest_day(day: date) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - HISTORY_MONTHS, 12)
    return date(year, month + 1, min(day.day, 28))


def _gaps(params: Params) -> list[str]:
    """Name the days of a custom range that Search Console cannot have."""
    if params.start and params.start < _oldest_day(today()):
        return [f"Search Console keeps {HISTORY_MONTHS} months, so older days have no data."]
    return []


def _partial_note(coverage: dict[str, Any]) -> list[str]:
    days = coverage["partial_days"]
    if not days:
        return []
    return [f"{days['from']} to {days['to']} are partial: they can change and are not a drop."]


def _filters(params: Params) -> dict[str, Any]:
    pairs = (("page", params.page), ("query", params.query))
    filters = [
        {"dimension": name, "operator": "equals", "expression": value}
        for name, value in pairs
        if value is not None
    ]
    return {"dimensionFilterGroups": [{"groupType": "and", "filters": filters}]} if filters else {}


async def _headers(site: Site) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _google.token(site, _google.GSC_SCOPE)}"}


async def _rows(
    site: Site,
    dimensions: list[str],
    headers: dict[str, str],
    window: dict[str, str],
    params: Params,
    state: str,
) -> list[dict[str, Any]]:
    body = {
        "startDate": window["start"],
        "endDate": window["end"],
        "dimensions": dimensions,
        "rowLimit": FETCH_ROWS,
        **_filters(params),
        **({"dataState": "all"} if state == "all" else {}),
    }
    return _flat(body, await _analytics(site, body, headers))


def _state(window: dict[str, str], last: date) -> str:
    """A range past the last complete day asks for fresh data: final data would drop those days."""
    return "all" if window["end"] > last.isoformat() else "final"


async def _analytics(
    site: Site, body: dict[str, Any], headers: dict[str, str], *, explain: bool = False
) -> Any:
    prop = quote(site.field("gsc_property"), safe="")
    url = f"{SITES_API}/{prop}/searchAnalytics/query"
    return await shared.request(
        site, ENDPOINTS, "POST", url, json=body, headers=headers, explain=explain, good=REPORT
    )


def _flat(body: dict[str, Any], reply: Any) -> Rows:
    dimensions = body.get("dimensions", [])
    return [
        {
            **dict(zip(dimensions, row.get("keys", []), strict=True)),
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": row["ctr"],
            "position": row["position"],
        }
        for row in reply.get("rows", [])
    ]


def _reported(reply: Any) -> date | None:
    """The docs give this field only for dataState all, grouped by date or by hour."""
    metadata = reply.get("metadata")
    found = metadata if isinstance(metadata, dict) else {}
    text = found.get("firstIncompleteDate") or found.get("firstIncompleteHour")
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text[:10]) - timedelta(days=1)
    except ValueError:
        return None


async def _last_complete(site: Site, headers: dict[str, str]) -> Known:
    """The docs give no other way: a report of final data has no metadata of its days.

    The body changes only with the UTC date, so all sources and agents of a site share this
    read. The report stays good without it, and then it ends on the estimate.
    """
    day = today()
    body = {
        "startDate": (day - timedelta(days=COVERAGE_DAYS)).isoformat(),
        "endDate": day.isoformat(),
        "dimensions": ["date"],
        "dataState": "all",
    }
    with suppress(net.ApiError, Refused):
        last = _reported(await _analytics(site, body, headers))
        if last:
            return last, "api", API_HOW
    return _estimate(day)


def _coverage(window: dict[str, str], known: Known, state: str) -> dict[str, Any]:
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    return _google.coverage(start, end, *known, ZONE, data_state=state)


def _shift(asked: date | None, sent: str) -> dict[str, Any]:
    return {"date_moved": moved(asked, sent)} if asked and asked.isoformat() != sent else {}


def _by_traffic(row: dict[str, Any]) -> tuple[int, int]:
    return row["clicks"], row["impressions"]


async def _report(
    site: Site,
    dimensions: list[str],
    params: Params,
    keep: Callable[[Rows], Rows] | None = None,
) -> dict[str, Any]:
    named = _named(params)
    headers = await _headers(site)
    # 1 boundary decides the default range, the data state, and the coverage.
    known = await _last_complete(site, headers)
    window = named or _default(params, known[0])
    state = _state(window, known[0])
    rows, totals = await asyncio.gather(
        _rows(site, dimensions, headers, window, params, state),
        _rows(site, [], headers, window, params, state),
    )
    if known[1] == "estimate":
        # A saved report keeps the estimate of its own call, also after midnight.
        known = _estimate(_google.answer_day())
    total = totals[0] if totals else {"clicks": 0, "impressions": 0}
    # Search Console hides rare queries, so the rows can sum to far less than the total.
    seen = min(total["impressions"], sum(row["impressions"] for row in rows))
    rows = keep(rows) if keep else sorted(rows, key=_by_traffic, reverse=True)
    coverage = _coverage(window, known, state)
    shift = _shift(params.end, window["end"])
    gaps = _gaps(params) + _partial_note(coverage)
    notes = gaps + ([MISS_REASON] if not rows and (params.page or params.query) else [])
    notes += [moved_note(item) for item in shift.values()]
    return ok(
        rows,
        thin=seen < THIN_IMPRESSIONS,
        reason=" ".join(notes) or None,
        window=window,
        totals=total,
        coverage=coverage,
        **({"incomplete": True} if gaps else {}),
        **shift,
    )


async def search_queries(site: Site, params: Params) -> dict[str, Any]:
    return await _report(site, ["query"], params)


async def search_pages(site: Site, params: Params) -> dict[str, Any]:
    return await _report(site, ["page"], params)


def _wins(rows: Rows) -> Rows:
    low, high = QUICK_WIN_POSITIONS
    near = [row for row in rows if low <= row["position"] <= high]
    return sorted(near, key=lambda row: row["impressions"], reverse=True)


async def quick_wins(site: Site, params: Params) -> dict[str, Any]:
    return await _report(site, ["query", "page"], params, _wins)


async def sitemaps(site: Site, params: Params) -> dict[str, Any]:
    prop = quote(site.field("gsc_property"), safe="")
    url = f"{SITES_API}/{prop}/sitemaps"
    headers = await _headers(site)
    reply = await shared.request(site, ENDPOINTS, "GET", url, headers=headers, good=SITEMAP_LIST)
    rows = [
        {
            "path": item["path"],
            "last_submitted": item.get("lastSubmitted"),
            "is_pending": item.get("isPending"),
            "errors": int(item.get("errors") or 0),
            "warnings": int(item.get("warnings") or 0),
            "submitted": sum(int(part.get("submitted") or 0) for part in item.get("contents", [])),
        }
        for item in reply.get("sitemap", [])
    ]
    return ok(rows, thin=not rows, reason=None if rows else "Search Console lists no sitemap.")


def _show_inspections(site: Site) -> str:
    key = site.field("gsc_property")
    quota.show(lambda: quota.view(LEDGER, key))
    return key


def _index_status(reply: Any) -> dict[str, Any]:
    """Read the status inside the failure path of _inspect: this call is already spent.

    Any other exception would pass shared.once with no failure window, and it would end the
    gather of the batch with other inspections in flight.
    """
    result = reply.get("inspectionResult") if isinstance(reply, dict) else None
    status = result.get("indexStatusResult", {}) if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise net.ApiError(200, net.BAD_SHAPE)
    return status


def _canonical_match(status: dict[str, Any]) -> bool | None:
    """Only 2 named canonicals can differ: a page with no declared canonical is no mismatch."""
    google, user = status.get("googleCanonical"), status.get("userCanonical")
    return google == user if google and user else None


async def _inspect(
    site: Site, url: str, headers: dict[str, str], spent: net.Receipt
) -> dict[str, Any]:
    body = {"inspectionUrl": url, "siteUrl": site.field("gsc_property")}
    try:
        reply = await net.request(
            ENDPOINTS, "POST", INSPECT_API, json=body, headers=headers, spent=spent
        )
        status = _index_status(reply)
    except net.ApiError as exc:
        return {"url": url, "error": exc.detail or str(exc), "failure": exc}
    return {
        "url": url,
        "verdict": status.get("verdict"),
        "coverage_state": status.get("coverageState"),
        "last_crawl_time": status.get("lastCrawlTime"),
        "canonical_match": _canonical_match(status),
    }


async def _top_urls(site: Site, headers: dict[str, str]) -> list[str]:
    # Only the top pages matter here, so the estimate is enough: it costs no coverage read.
    last = _estimate(today())[0]
    rows = await _rows(site, ["page"], headers, _default(NONE, last), NONE, "final")
    pages = sorted(rows, key=_by_traffic, reverse=True)
    home = f"https://{site.host}/"
    return list(dict.fromkeys([home, *(row["page"] for row in pages)]))[:INSPECT_URLS]


async def index_status(site: Site, params: Params) -> dict[str, Any]:
    headers = await _headers(site)
    urls = [params.page] if params.page else await _top_urls(site, headers)
    key = _show_inspections(site)

    async def prepare() -> net.Receipt:
        return await quota.spend(LEDGER, key, len(urls))

    async def send(spent: net.Receipt) -> dict[str, Any]:
        found = list(await asyncio.gather(*(_inspect(site, url, headers, spent) for url in urls)))
        failures = [row.pop("failure") for row in found if "failure" in row]
        if len(failures) == len(found):
            # The batch spent, so it keeps the failure window. A sent failure says more.
            raise next((failure for failure in failures if failure.sent), failures[0])
        return {"rows": found}

    # 1 failed URL of many stays in the saved answer: a repeat must not spend all URLs again.
    path = shared.entry(site, "index_status", key, sorted(urls))
    rows = (await shared.once(path, send, prepare=prepare))["rows"]
    bad = [row for row in rows if "error" in row]
    reason = f"{len(bad)} of {len(rows)} URLs failed inspection." if bad else None
    enough = 1 if params.page else THIN_INSPECTED_URLS
    return ok(rows, thin=len(rows) - len(bad) < enough, reason=reason)


SOURCES = {
    "search_queries": search_queries,
    "search_pages": search_pages,
    "quick_wins": quick_wins,
    "sitemaps": sitemaps,
    "index_status": index_status,
}
ANALYTICS = DATES | {"last_days", "page", "query"}
ACCEPTS = {
    "search_queries": ANALYTICS,
    "search_pages": ANALYTICS,
    "quick_wins": ANALYTICS,
    "sitemaps": frozenset[str](),
    "index_status": frozenset({"page"}),
}

DIMENSIONS = ("country", "device", "page", "query", "searchAppearance", "date", "hour")
FILTER_DIMENSIONS = DIMENSIONS[:5]
OPERATORS = ("equals", "notEquals", "contains", "notContains", "includingRegex", "excludingRegex")
TYPES = ("web", "image", "video", "news", "discover", "googleNews")
AGGREGATIONS = ("auto", "byPage", "byProperty", "byNewsShowcasePanel")
DATA_STATES = ("final", "all", "hourly_all")


def _check_filter(item: Any) -> None:
    if not isinstance(item, dict):
        raise BadInput("Each filter of dimensionFilterGroups must be an object.")
    unknown = sorted(set(item) - {"dimension", "operator", "expression"})
    if unknown:
        raise BadInput(f"A filter has no field {', '.join(unknown)}.")
    one_of("A filter dimension", item.get("dimension"), FILTER_DIMENSIONS)
    one_of("A filter operator", item.get("operator", "equals"), OPERATORS)
    expression = item.get("expression")
    if not isinstance(expression, str) or len(expression) > MAX_TEXT:
        raise BadInput(f"A filter expression must be text of at most {MAX_TEXT} characters.")


def _check_groups(groups: Any) -> None:
    if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
        raise BadInput("dimensionFilterGroups must be a list of objects.")
    for group in groups:
        unknown = sorted(set(group) - {"groupType", "filters"})
        if unknown:
            raise BadInput(f"A filter group has no field {', '.join(unknown)}.")
        one_of("groupType", group.get("groupType", "and"), ("and",))
        filters = group.get("filters")
        if not isinstance(filters, list):
            raise BadInput("Each filter group needs a filters list.")
        for item in filters:
            _check_filter(item)


async def _search_analytics(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    start, end = period("startDate", "endDate", request["startDate"], request["endDate"])
    dimensions = request.get("dimensions", [])
    if not isinstance(dimensions, list):
        raise BadInput("dimensions must be a list.")
    for name in dimensions:
        one_of("Each of dimensions", name, DIMENSIONS)
    for field, allowed in (
        ("type", TYPES),
        ("aggregationType", AGGREGATIONS),
        ("dataState", DATA_STATES),
    ):
        if field in request:
            one_of(field, request[field], allowed)
    _check_groups(request.get("dimensionFilterGroups", []))
    whole("startRow", request.get("startRow", 0), 0, 2**31 - 1)
    limit = row_limit("rowLimit", request.get("rowLimit"), "startRow")
    sent = _newest_end("startDate", start, end) if start and end else end
    body = {**request, "rowLimit": limit, "endDate": str(sent)}
    reply = await _analytics(site, body, await _headers(site), explain=True)
    rows = _flat(body, reply)
    # 1 method sends 1 query, so the value comes only from this reply.
    last = _reported(reply)
    known = (last, "api", API_HOW) if last else (*_estimate(_google.answer_day())[:2], ASK_HOW)
    window = {"start": request["startDate"], "end": body["endDate"]}
    coverage = _coverage(window, known, str(request.get("dataState", "final")))
    shift = _shift(end, body["endDate"])
    gaps = _gaps(Params(start, end)) + _partial_note(coverage)
    notes = gaps + [moved_note(item) for item in shift.values()]
    return answer(
        rows,
        more_rows="possible" if len(rows) == limit else "no",
        reason=" ".join(notes) or None,
        applied=body,
        coverage=coverage,
        **({"incomplete": True} if gaps else {}),
        **shift,
    )


async def _url_inspection(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    url = page_url(site.host, request["inspectionUrl"], "inspectionUrl")
    body = {**request, "inspectionUrl": url, "siteUrl": site.field("gsc_property")}
    headers = await _headers(site)
    key = _show_inspections(site)

    async def prepare() -> net.Receipt:
        return await quota.spend(LEDGER, key)

    async def send(spent: net.Receipt) -> Any:
        return await net.request(
            ENDPOINTS, "POST", INSPECT_API, json=body, headers=headers, explain=True, spent=spent
        )

    def good(reply: Any) -> bool:
        return shared.is_object(reply) and isinstance(reply.get("inspectionResult"), dict)

    path = shared.entry(site, "url_inspection", body)
    reply = await shared.once(path, send, prepare=prepare, good=good)
    return answer([reply["inspectionResult"]], more_rows="no", applied=body)


async def _sitemap_list(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = quote(site.field("gsc_property"), safe="")
    query = {}
    if "sitemapIndex" in request:
        query["sitemapIndex"] = page_url(site.host, request["sitemapIndex"], "sitemapIndex")
    url = f"{SITES_API}/{prop}/sitemaps"
    headers = await _headers(site)
    reply = await shared.request(
        site, ENDPOINTS, "GET", url, params=query, headers=headers, explain=True, good=SITEMAP_LIST
    )
    return answer(reply.get("sitemap", []), more_rows="no", applied=query)


async def _sitemap_get(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = quote(site.field("gsc_property"), safe="")
    feed = page_url(site.host, request["feedpath"], "feedpath")
    url = f"{SITES_API}/{prop}/sitemaps/{quote(feed, safe='')}"
    headers = await _headers(site)
    reply = await shared.request(
        site, ENDPOINTS, "GET", url, headers=headers, explain=True, good=SITEMAP
    )
    return answer([reply], more_rows="no", applied={"feedpath": feed})


def now(site: Site) -> dict[str, Any]:
    return {"url_inspection": quota.view(LEDGER, site.field("gsc_property"))}


APIS = {
    "search_console": Api(
        title="Google Search Console",
        summary="Google Search clicks, rankings, URL index status, and sitemaps.",
        docs="https://developers.google.com/webmaster-tools/v1/api_reference_index",
        quota=Quota(
            "https://developers.google.com/webmaster-tools/limits",
            (
                f"URL Inspection: {LEDGER.limit} queries for each site each day, 600 each "
                "minute. For each project: 10,000,000 each day, 15,000 each minute.",
                "Search Analytics: 1,200 queries each minute for each site, and for each user. "
                "For each project: 30,000,000 each day, 40,000 each minute. The load quota "
                "has no number: wide date ranges and page or query grouping cost more.",
                "All other resources: 20 each second and 200 each minute for each user. "
                "For each project: 100,000,000 each day.",
                "The docs state no reset time. The server assumes 00:00 UTC.",
                "The server counts only URL Inspection: a count of the other limits changes "
                "no decision, and it does not enforce the limit of each minute.",
            ),
            frozenset({"gsc_property", "google_service_account"}),
            now,
        ),
        methods={
            "search_analytics": Method(
                _search_analytics,
                "The Performance report: clicks, impressions, CTR, and position.",
                frozenset(
                    {
                        "startDate",
                        "endDate",
                        "dimensions",
                        "type",
                        "dimensionFilterGroups",
                        "aggregationType",
                        "rowLimit",
                        "startRow",
                        "dataState",
                    }
                ),
                frozenset({"startDate", "endDate"}),
            ),
            "url_inspection": Method(
                _url_inspection,
                "The Google index state of 1 URL.",
                frozenset({"inspectionUrl", "languageCode"}),
                frozenset({"inspectionUrl"}),
            ),
            "sitemaps": Method(
                _sitemap_list, "The sitemaps of the property.", frozenset({"sitemapIndex"})
            ),
            "sitemap": Method(
                _sitemap_get,
                "1 sitemap.",
                frozenset({"feedpath"}),
                frozenset({"feedpath"}),
            ),
        },
        options={
            "dimensions": list(DIMENSIONS),
            "filter_dimensions": list(FILTER_DIMENSIONS),
            "operators": list(OPERATORS),
            "types": list(TYPES),
            "aggregation_types": list(AGGREGATIONS),
            "data_states": list(DATA_STATES),
            "group_types": ["and"],
            "dates": "YYYY-MM-DD in Pacific Time, inclusive.",
            "max_filter_text": MAX_TEXT,
            "row_limit": "1 to 1000 for each call here. Read more rows with startRow.",
        },
        gaps=(
            "Page indexing, Core Web Vitals, Links, Manual actions, and Security issues "
            "reports: the API has no method for them.",
            "History older than 16 months.",
            "Rare queries: Search Console hides them, so rows can sum to less than the totals.",
            "More than 50,000 rows for each day and search type: the API stops there, "
            "sorted by clicks.",
            "The live URL test: URL Inspection gives only the state in the Google index.",
        ),
    )
}
