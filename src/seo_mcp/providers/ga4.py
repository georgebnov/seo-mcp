"""Google Analytics 4: organic traffic and organic landing pages."""

from typing import Any
from urllib.parse import urlsplit

from seo_mcp import net
from seo_mcp.api import Api, Method, Run, answer, row_limit, whole
from seo_mcp.config import Site
from seo_mcp.params import DATES, Params
from seo_mcp.providers import _google
from seo_mcp.results import ok

HOST = "analyticsdata.googleapis.com"
ENDPOINTS = (
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runReport"),
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runPivotReport"),
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runRealtimeReport"),
    net.Endpoint("GET", HOST, r"/v1beta/properties/\d+/metadata"),
)
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
    reply = await net.request(ENDPOINTS, "POST", url, json=body, headers=headers)
    names = [header["name"] for header in reply.get("metricHeaders", [])]
    rows = [
        {
            dimension: row["dimensionValues"][0]["value"],
            **{
                name: float(cell["value"])
                for name, cell in zip(names, row["metricValues"], strict=True)
            },
        }
        for row in reply.get("rows", [])
    ]
    # GA4 leaves out rows and totals when the window has no sessions.
    total_cells = (reply.get("totals") or [{}])[0].get("metricValues", [])
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

SHARED_FIELDS = frozenset(
    {
        "dimensions",
        "metrics",
        "dimensionFilter",
        "metricFilter",
        "returnPropertyQuota",
    }
)
HISTORY_FIELDS = SHARED_FIELDS | {
    "dateRanges",
    "currencyCode",
    "cohortSpec",
    "keepEmptyRows",
    "comparisons",
}
LIST_FIELDS = frozenset({"limit", "metricAggregations", "orderBys"})
PASSED_ON = ("metadata", "propertyQuota", "pivotHeaders", "aggregates")


def _flat_row(row: dict[str, Any], dimensions: list[str], metrics: list[str]) -> dict[str, Any]:
    cells = zip(metrics, row.get("metricValues", []), strict=False)
    labels = zip(dimensions, row.get("dimensionValues", []), strict=False)
    return {
        **{name: cell.get("value") for name, cell in labels},
        **{name: _number(cell.get("value")) for name, cell in cells},
    }


def _number(text: Any) -> Any:
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def _flat(reply: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dimensions = [header["name"] for header in reply.get("dimensionHeaders", [])]
    metrics = [header["name"] for header in reply.get("metricHeaders", [])]
    return [_flat_row(row, dimensions, metrics) for row in rows]


def _reader(call: str, paging: bool, limits: bool = True) -> Run:
    async def run(site: Site, request: dict[str, Any]) -> dict[str, Any]:
        prop = site.field("ga4_property_id")
        body = dict(request)
        offset = 0
        if limits:
            body["limit"] = row_limit(
                "limit", request.get("limit"), "offset" if paging else "a filter"
            )
        if "offset" in request:
            offset = whole("offset", request["offset"], 0, 2**53)
        token = await _google.token(site, _google.GA4_SCOPE)
        reply = await net.request(
            ENDPOINTS,
            "POST",
            f"https://{HOST}/v1beta/properties/{prop}:{call}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            explain=True,
        )
        rows = _flat(reply, reply.get("rows", []))
        count = reply.get("rowCount")
        more = "possible" if count is None else "yes" if offset + len(rows) < count else "no"
        extra = {name: reply[name] for name in PASSED_ON if name in reply}
        for name in ("totals", "maximums", "minimums"):
            if name in reply:
                extra[name] = _flat(reply, reply[name])
        return answer(rows, more_rows=more, row_count=count, applied=body, **extra)

    return run


async def _metadata(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    token = await _google.token(site, _google.GA4_SCOPE)
    url = f"https://{HOST}/v1beta/properties/{prop}/metadata"
    headers = {"Authorization": f"Bearer {token}"}
    reply = await net.request(ENDPOINTS, "GET", url, headers=headers, explain=True)

    def brief(kind: str, item: dict[str, Any]) -> dict[str, Any]:
        row = {"kind": kind, "apiName": item.get("apiName")}
        if item.get("category"):
            row["category"] = item["category"]
        if item.get("customDefinition"):
            row["custom"] = True
        return row

    rows = [brief("dimension", item) for item in reply.get("dimensions", [])]
    rows += [brief("metric", item) for item in reply.get("metrics", [])]
    return answer(rows, more_rows="no", row_count=len(rows))


APIS = {
    "ga4": Api(
        title="Google Analytics 4 Data API",
        docs="https://developers.google.com/analytics/devguides/reporting/data/v1/rest",
        methods={
            "run_report": Method(
                _reader("runReport", paging=True),
                "A report. dateRanges takes 2 periods for a comparison.",
                HISTORY_FIELDS | LIST_FIELDS | {"offset"},
            ),
            "pivot_report": Method(
                _reader("runPivotReport", paging=False, limits=False),
                "A pivot report. Each pivot has its own limit.",
                HISTORY_FIELDS | {"pivots"},
            ),
            "realtime_report": Method(
                _reader("runRealtimeReport", paging=False),
                "The last 30 minutes.",
                SHARED_FIELDS | LIST_FIELDS | {"minuteRanges"},
            ),
            "metadata": Method(
                _metadata, "All dimensions and metrics of this property, with custom ones."
            ),
        },
        options={
            "dimensions_and_metrics": "Call the method metadata: it lists the live names.",
            "filters": "dimensionFilter and metricFilter take a FilterExpression: andGroup, "
            "orGroup, notExpression, or filter. A filter has fieldName and 1 of stringFilter, "
            "inListFilter, numericFilter, betweenFilter, or emptyFilter.",
            "string_match_types": [
                "EXACT",
                "BEGINS_WITH",
                "ENDS_WITH",
                "CONTAINS",
                "FULL_REGEXP",
                "PARTIAL_REGEXP",
            ],
            "dates": "YYYY-MM-DD, NdaysAgo, yesterday, or today.",
            "row_limit": "1 to 1000 for each call here. Read more rows with offset.",
        },
        gaps=(
            "Explorations and funnel reports: the v1beta API has no method for them, and the "
            "funnel method is only in v1alpha.",
            "Rows below the thresholds of GA4: metadata.subjectToThresholding shows this.",
            "Rows rolled into (other): metadata.dataLossFromOtherRow shows this.",
        ),
    )
}
