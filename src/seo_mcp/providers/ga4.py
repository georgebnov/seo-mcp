"""Google Analytics 4: organic traffic and organic landing pages."""

from typing import Any
from urllib.parse import urlsplit

from seo_mcp import net
from seo_mcp.config import Site
from seo_mcp.params import DATES, Params
from seo_mcp.providers import _google
from seo_mcp.results import ok

HOST = "analyticsdata.googleapis.com"
ENDPOINTS = (net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runReport"),)
WINDOW = {"startDate": "28daysAgo", "endDate": "yesterday"}
WINDOW_DAYS = 28
THIN_SESSIONS = 100
MAX_DAILY_ROWS = 100
PAGE_MATCH = "Sessions that start on this exact path, on every host of the property."
METRICS = ("sessions", "totalUsers", "engagementRate", "keyEvents")
ORGANIC = {
    "filter": {
        "fieldName": "sessionDefaultChannelGroup",
        "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
    }
}


def _window(params: Params) -> dict[str, str]:
    if params.start and params.end:
        return {"startDate": params.start.isoformat(), "endDate": params.end.isoformat()}
    if params.last_days:
        return {"startDate": f"{params.last_days}daysAgo", "endDate": "yesterday"}
    return WINDOW


def _days(params: Params) -> int:
    if params.start and params.end:
        return (params.end - params.start).days + 1
    return params.last_days or WINDOW_DAYS


def _filter(params: Params) -> dict[str, Any]:
    if not params.page:
        return ORGANIC
    parts = urlsplit(params.page)
    # landingPage has no query string, so a page with one needs the other dimension.
    field = "landingPagePlusQueryString" if parts.query else "landingPage"
    value = f"{parts.path}?{parts.query}" if parts.query else parts.path
    page = {"fieldName": field, "stringFilter": {"matchType": "EXACT", "value": value}}
    return {"andGroup": {"expressions": [ORGANIC, {"filter": page}]}}


async def _report(
    site: Site, dimension: str, limit: int, fetch: int, params: Params
) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    token = await _google.token(site, _google.GA4_SCOPE)
    window = _window(params)
    body = {
        "dateRanges": [window],
        "dimensions": [{"name": dimension}],
        "metrics": [{"name": name} for name in METRICS],
        "dimensionFilter": _filter(params),
        "metricAggregations": ["TOTAL"],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": fetch,
    }
    url = f"https://{HOST}/v1beta/properties/{prop}:runReport"
    headers = {"Authorization": f"Bearer {token}"}
    answer = await net.request(ENDPOINTS, "POST", url, json=body, headers=headers)
    names = [header["name"] for header in answer.get("metricHeaders", [])]
    rows = [
        {
            dimension: row["dimensionValues"][0]["value"],
            **{
                name: float(cell["value"])
                for name, cell in zip(names, row["metricValues"], strict=True)
            },
        }
        for row in answer.get("rows", [])
    ]
    # GA4 leaves out rows and totals when the window has no sessions.
    total_cells = (answer.get("totals") or [{}])[0].get("metricValues", [])
    totals = {name: float(cell["value"]) for name, cell in zip(names, total_cells, strict=False)}
    reason = None if rows else "GA4 reports no organic sessions in the window."
    thin = totals.get("sessions", 0) < THIN_SESSIONS
    extra = {"page_match": PAGE_MATCH} if params.page else {}
    return ok(rows, thin=thin, reason=reason, limit=limit, window=window, totals=totals, **extra)


async def ga4_organic(site: Site, params: Params) -> dict[str, Any]:
    limit = min(_days(params), MAX_DAILY_ROWS)
    # 1 more row than the limit shows that a long range has more days than the rows.
    result = await _report(site, "date", limit, limit + 1, params)
    result["rows"].sort(key=lambda row: row["date"])
    return result


async def ga4_landing_pages(site: Site, params: Params) -> dict[str, Any]:
    return await _report(site, "landingPage", 25, 25, params)


SOURCES = {"ga4_organic": ga4_organic, "ga4_landing_pages": ga4_landing_pages}
ACCEPTS = {name: DATES | {"last_days", "page"} for name in SOURCES}
