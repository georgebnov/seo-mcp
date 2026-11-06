"""Google Search Console: queries, pages, quick wins, sitemaps, and index status."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from seo_mcp import net
from seo_mcp.config import Site
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


def _window() -> dict[str, str]:
    end = datetime.now(UTC).date() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return {"start": start.isoformat(), "end": end.isoformat()}


async def _headers(site: Site) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _google.token(site, _google.GSC_SCOPE)}"}


async def _rows(site: Site, dimensions: list[str], headers: dict[str, str]) -> list[dict[str, Any]]:
    window = _window()
    prop = quote(site.field("gsc_property"), safe="")
    body = {
        "startDate": window["start"],
        "endDate": window["end"],
        "dimensions": dimensions,
        "rowLimit": FETCH_ROWS,
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
    site: Site, dimensions: list[str], keep: Callable[[Rows], Rows] | None = None
) -> dict[str, Any]:
    headers = await _headers(site)
    rows, totals = await asyncio.gather(_rows(site, dimensions, headers), _rows(site, [], headers))
    total = totals[0] if totals else {"clicks": 0, "impressions": 0}
    # Search Console hides rare queries, so the rows can sum to far less than the total.
    seen = min(total["impressions"], sum(row["impressions"] for row in rows))
    rows = keep(rows) if keep else sorted(rows, key=_by_traffic, reverse=True)
    return ok(rows, thin=seen < THIN_IMPRESSIONS, window=_window(), totals=total)


async def search_queries(site: Site) -> dict[str, Any]:
    return await _report(site, ["query"])


async def search_pages(site: Site) -> dict[str, Any]:
    return await _report(site, ["page"])


def _wins(rows: Rows) -> Rows:
    low, high = QUICK_WIN_POSITIONS
    near = [row for row in rows if low <= row["position"] <= high]
    return sorted(near, key=lambda row: row["impressions"], reverse=True)


async def quick_wins(site: Site) -> dict[str, Any]:
    return await _report(site, ["query", "page"], _wins)


async def sitemaps(site: Site) -> dict[str, Any]:
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


async def index_status(site: Site) -> dict[str, Any]:
    headers = await _headers(site)
    pages = sorted(await _rows(site, ["page"], headers), key=_by_traffic, reverse=True)
    home = f"https://{site.host}/"
    urls = list(dict.fromkeys([home, *(row["page"] for row in pages)]))[:INSPECT_URLS]
    rows = list(await asyncio.gather(*(_inspect(site, url, headers) for url in urls)))
    bad = [row for row in rows if "error" in row]
    if len(bad) == len(rows):
        raise net.ApiError(bad[0]["status"])
    for row in bad:
        del row["status"]
    reason = f"{len(bad)} of {len(rows)} URLs failed inspection." if bad else None
    return ok(rows, thin=len(rows) - len(bad) < THIN_INSPECTED_URLS, reason=reason)


SOURCES = {
    "search_queries": search_queries,
    "search_pages": search_pages,
    "quick_wins": quick_wins,
    "sitemaps": sitemaps,
    "index_status": index_status,
}
