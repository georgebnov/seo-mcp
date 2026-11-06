"""Google Search Console: queries, pages, quick wins, sitemaps, and index status."""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from seo_mcp import net, quota
from seo_mcp.api import Api, Method, Quota, answer, one_of, row_limit, whole
from seo_mcp.config import Site
from seo_mcp.params import DATES, MAX_TEXT, NONE, BadInput, Params, page_url, period
from seo_mcp.providers import _google
from seo_mcp.results import ok

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
# Search Console data ends about 3 days before today.
LAG_DAYS = 3
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


def _window(params: Params) -> dict[str, str]:
    if params.start and params.end:
        return {"start": params.start.isoformat(), "end": params.end.isoformat()}
    end = datetime.now(UTC).date() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=(params.last_days or WINDOW_DAYS) - 1)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _oldest_day(today: date) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 - HISTORY_MONTHS, 12)
    return date(year, month + 1, min(today.day, 28))


def _gaps(params: Params) -> list[str]:
    """Name the days of a custom range that Search Console cannot have."""
    today = datetime.now(UTC).date()
    gaps = []
    if params.start and params.start < _oldest_day(today):
        gaps.append(f"Search Console keeps {HISTORY_MONTHS} months, so older days have no data.")
    if params.end and params.end > today - timedelta(days=LAG_DAYS):
        gaps.append(f"Search Console data ends about {LAG_DAYS} days before today.")
    return gaps


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
) -> list[dict[str, Any]]:
    body = {
        "startDate": window["start"],
        "endDate": window["end"],
        "dimensions": dimensions,
        "rowLimit": FETCH_ROWS,
        **_filters(params),
    }
    return await _analytics(site, body, headers)


async def _analytics(
    site: Site, body: dict[str, Any], headers: dict[str, str], *, explain: bool = False
) -> Rows:
    prop = quote(site.field("gsc_property"), safe="")
    dimensions = body.get("dimensions", [])
    url = f"{SITES_API}/{prop}/searchAnalytics/query"
    reply = await net.request(ENDPOINTS, "POST", url, json=body, headers=headers, explain=explain)
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


def _by_traffic(row: dict[str, Any]) -> tuple[int, int]:
    return row["clicks"], row["impressions"]


async def _report(
    site: Site,
    dimensions: list[str],
    params: Params,
    keep: Callable[[Rows], Rows] | None = None,
) -> dict[str, Any]:
    headers = await _headers(site)
    window = _window(params)
    rows, totals = await asyncio.gather(
        _rows(site, dimensions, headers, window, params),
        _rows(site, [], headers, window, params),
    )
    total = totals[0] if totals else {"clicks": 0, "impressions": 0}
    # Search Console hides rare queries, so the rows can sum to far less than the total.
    seen = min(total["impressions"], sum(row["impressions"] for row in rows))
    rows = keep(rows) if keep else sorted(rows, key=_by_traffic, reverse=True)
    gaps = _gaps(params)
    notes = gaps + ([MISS_REASON] if not rows and (params.page or params.query) else [])
    return ok(
        rows,
        thin=seen < THIN_IMPRESSIONS,
        reason=" ".join(notes) or None,
        window=window,
        totals=total,
        **({"incomplete": True} if gaps else {}),
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
    reply = await net.request(ENDPOINTS, "GET", url, headers=await _headers(site))
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


async def _inspections(site: Site, count: int) -> net.Receipt:
    key = site.field("gsc_property")
    quota.show(lambda: quota.view(LEDGER, key))
    return await quota.spend(LEDGER, key, count)


async def _inspect(
    site: Site, url: str, headers: dict[str, str], spent: net.Receipt
) -> dict[str, Any]:
    body = {"inspectionUrl": url, "siteUrl": site.field("gsc_property")}
    try:
        reply = await net.request(
            ENDPOINTS, "POST", INSPECT_API, json=body, headers=headers, spent=spent
        )
    except net.ApiError as exc:
        return {"url": url, "error": str(exc), "status": exc.status}
    status = reply.get("inspectionResult", {}).get("indexStatusResult", {})
    return {
        "url": url,
        "verdict": status.get("verdict"),
        "coverage_state": status.get("coverageState"),
        "last_crawl_time": status.get("lastCrawlTime"),
        "canonical_match": status.get("googleCanonical") == status.get("userCanonical"),
    }


async def _top_urls(site: Site, headers: dict[str, str]) -> list[str]:
    rows = await _rows(site, ["page"], headers, _window(NONE), NONE)
    pages = sorted(rows, key=_by_traffic, reverse=True)
    home = f"https://{site.host}/"
    return list(dict.fromkeys([home, *(row["page"] for row in pages)]))[:INSPECT_URLS]


async def index_status(site: Site, params: Params) -> dict[str, Any]:
    headers = await _headers(site)
    urls = [params.page] if params.page else await _top_urls(site, headers)
    spent = await _inspections(site, len(urls))
    rows = list(await asyncio.gather(*(_inspect(site, url, headers, spent) for url in urls)))
    bad = [row for row in rows if "error" in row]
    if len(bad) == len(rows):
        raise net.ApiError(bad[0]["status"])
    for row in bad:
        del row["status"]
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
    body = {**request, "rowLimit": limit}
    rows = await _analytics(site, body, await _headers(site), explain=True)
    gaps = _gaps(Params(start, end))
    return answer(
        rows,
        more_rows="possible" if len(rows) == limit else "no",
        reason=" ".join(gaps) or None,
        applied=body,
        **({"incomplete": True} if gaps else {}),
    )


async def _url_inspection(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    url = page_url(site.host, request["inspectionUrl"], "inspectionUrl")
    body = {**request, "inspectionUrl": url, "siteUrl": site.field("gsc_property")}
    headers = await _headers(site)
    spent = await _inspections(site, 1)
    reply = await net.request(
        ENDPOINTS, "POST", INSPECT_API, json=body, headers=headers, explain=True, spent=spent
    )
    return answer([reply.get("inspectionResult", {})], more_rows="no", applied=body)


async def _sitemap_list(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = quote(site.field("gsc_property"), safe="")
    query = {}
    if "sitemapIndex" in request:
        query["sitemapIndex"] = page_url(site.host, request["sitemapIndex"], "sitemapIndex")
    url = f"{SITES_API}/{prop}/sitemaps"
    headers = await _headers(site)
    reply = await net.request(ENDPOINTS, "GET", url, params=query, headers=headers, explain=True)
    return answer(reply.get("sitemap", []), more_rows="no", applied=query)


async def _sitemap_get(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = quote(site.field("gsc_property"), safe="")
    feed = page_url(site.host, request["feedpath"], "feedpath")
    url = f"{SITES_API}/{prop}/sitemaps/{quote(feed, safe='')}"
    reply = await net.request(ENDPOINTS, "GET", url, headers=await _headers(site), explain=True)
    return answer([reply], more_rows="no", applied={"feedpath": feed})


def now(site: Site) -> dict[str, Any]:
    return {"url_inspection": quota.view(LEDGER, site.field("gsc_property"))}


APIS = {
    "search_console": Api(
        title="Google Search Console",
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
