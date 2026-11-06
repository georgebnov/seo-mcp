"""Google Search Console: queries, pages, quick wins, sitemaps, and index status."""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from seo_mcp import net
from seo_mcp.config import Site
from seo_mcp.params import DATES, NONE, Params
from seo_mcp.providers import _google
from seo_mcp.results import ok

Rows = list[dict[str, Any]]

HOST = "searchconsole.googleapis.com"
SITES_API = f"https://{HOST}/webmasters/v3/sites"
INSPECT_API = f"https://{HOST}/v1/urlInspection/index:inspect"
ENDPOINTS = (
    net.Endpoint("POST", HOST, r"/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    net.Endpoint("GET", HOST, r"/webmasters/v3/sites/[^/]+/sitemaps"),
    net.Endpoint("POST", HOST, r"/v1/urlInspection/index:inspect"),
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
    prop = quote(site.field("gsc_property"), safe="")
    body = {
        "startDate": window["start"],
        "endDate": window["end"],
        "dimensions": dimensions,
        "rowLimit": FETCH_ROWS,
        **_filters(params),
    }
    url = f"{SITES_API}/{prop}/searchAnalytics/query"
    answer = await net.request(ENDPOINTS, "POST", url, json=body, headers=headers)
    return [
        {
            **dict(zip(dimensions, row.get("keys", []), strict=True)),
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": row["ctr"],
            "position": row["position"],
        }
        for row in answer.get("rows", [])
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
    answer = await net.request(ENDPOINTS, "GET", url, headers=await _headers(site))
    rows = [
        {
            "path": item["path"],
            "last_submitted": item.get("lastSubmitted"),
            "is_pending": item.get("isPending"),
            "errors": int(item.get("errors") or 0),
            "warnings": int(item.get("warnings") or 0),
            "submitted": sum(int(part.get("submitted") or 0) for part in item.get("contents", [])),
        }
        for item in answer.get("sitemap", [])
    ]
    return ok(rows, thin=not rows, reason=None if rows else "Search Console lists no sitemap.")


async def _inspect(site: Site, url: str, headers: dict[str, str]) -> dict[str, Any]:
    body = {"inspectionUrl": url, "siteUrl": site.field("gsc_property")}
    try:
        answer = await net.request(ENDPOINTS, "POST", INSPECT_API, json=body, headers=headers)
    except net.ApiError as exc:
        return {"url": url, "error": str(exc), "status": exc.status}
    status = answer.get("inspectionResult", {}).get("indexStatusResult", {})
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
    rows = list(await asyncio.gather(*(_inspect(site, url, headers) for url in urls)))
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
