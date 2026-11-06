"""Google Analytics 4: organic traffic and organic landing pages."""

from typing import Any

from seo_mcp import net
from seo_mcp.config import Site
from seo_mcp.providers import _google
from seo_mcp.results import ok

HOST = "analyticsdata.googleapis.com"
ENDPOINTS = (net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runReport"),)
WINDOW = {"startDate": "28daysAgo", "endDate": "yesterday"}
WINDOW_DAYS = 28
THIN_SESSIONS = 100
METRICS = ("sessions", "totalUsers", "engagementRate", "keyEvents")
ORGANIC = {
    "filter": {
        "fieldName": "sessionDefaultChannelGroup",
        "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
    }
}


async def _report(site: Site, dimension: str, limit: int) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    token = await _google.token(site, _google.GA4_SCOPE)
    body = {
        "dateRanges": [WINDOW],
        "dimensions": [{"name": dimension}],
        "metrics": [{"name": name} for name in METRICS],
        "dimensionFilter": ORGANIC,
        "metricAggregations": ["TOTAL"],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": limit,
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
    return ok(rows, thin=thin, reason=reason, limit=limit, window=WINDOW, totals=totals)


async def ga4_organic(site: Site) -> dict[str, Any]:
    result = await _report(site, "date", WINDOW_DAYS + 1)
    result["rows"].sort(key=lambda row: row["date"])
    return result


async def ga4_landing_pages(site: Site) -> dict[str, Any]:
    return await _report(site, "landingPage", 25)


SOURCES = {"ga4_organic": ga4_organic, "ga4_landing_pages": ga4_landing_pages}
