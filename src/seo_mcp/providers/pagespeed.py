"""PageSpeed Insights (lab data) and the Chrome UX Report (field data)."""

from typing import Any

from seo_mcp import net
from seo_mcp.api import Api, Method, Run, answer, one_of, whole
from seo_mcp.config import Site
from seo_mcp.params import BadInput, Params, page_url
from seo_mcp.results import ok

PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_API = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
ENDPOINTS = (
    net.Endpoint("GET", "www.googleapis.com", r"/pagespeedonline/v5/runPagespeed"),
    net.Endpoint("POST", "chromeuxreport.googleapis.com", r"/v1/records:queryRecord"),
    net.Endpoint("POST", "chromeuxreport.googleapis.com", r"/v1/records:queryHistoryRecord"),
)
CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
LAB_METRICS = (
    "first-contentful-paint",
    "largest-contentful-paint",
    "total-blocking-time",
    "cumulative-layout-shift",
    "speed-index",
)
MAX_OPPORTUNITIES = 5
# Web Vitals limits: p75 up to the first value is good, over the second value is poor.
RATINGS = {
    "largest_contentful_paint": (2500, 4000),
    "interaction_to_next_paint": (200, 500),
    "cumulative_layout_shift": (0.1, 0.25),
    "first_contentful_paint": (1800, 3000),
    "experimental_time_to_first_byte": (800, 1800),
}


def _key_header(key: str | None) -> dict[str, str]:
    return {"X-Goog-Api-Key": key} if key else {}


async def page_speed(site: Site, params: Params) -> dict[str, Any]:
    query = [("url", params.page or f"https://{site.host}/"), ("strategy", "mobile")]
    query += [("category", name) for name in CATEGORIES]
    headers = _key_header(site.optional_secret("google_api_key"))
    reply = await net.request(ENDPOINTS, "GET", PSI_API, params=query, headers=headers)
    lighthouse = reply["lighthouseResult"]
    audits = lighthouse.get("audits", {})
    rows: list[dict[str, Any]] = [
        {"kind": "score", "metric": name, "value": round(item["score"] * 100)}
        for name, item in lighthouse.get("categories", {}).items()
        if item.get("score") is not None
    ]
    rows += [
        {
            "kind": "lab",
            "metric": name,
            "value": audits[name].get("numericValue"),
            "display": audits[name].get("displayValue"),
        }
        for name in LAB_METRICS
        if name in audits
    ]
    savings = [
        {
            "kind": "opportunity",
            "metric": name,
            "value": audit["details"]["overallSavingsMs"],
            "display": audit.get("title"),
        }
        for name, audit in audits.items()
        if (audit.get("details") or {}).get("overallSavingsMs")
    ]
    savings.sort(key=lambda row: row["value"], reverse=True)
    rows += savings[:MAX_OPPORTUNITIES]
    window = {"lab_run": lighthouse.get("fetchTime"), "strategy": "mobile"}
    return ok(rows, thin=not rows, window=window)


def _rating(metric: str, p75: float) -> str | None:
    if metric not in RATINGS:
        return None
    good, poor = RATINGS[metric]
    if p75 <= good:
        return "good"
    return "needs-improvement" if p75 <= poor else "poor"


async def chrome_field_data(site: Site, params: Params) -> dict[str, Any]:
    headers = _key_header(site.secret("google_api_key"))
    body = {"url": params.page} if params.page else {"origin": f"https://{site.host}"}
    try:
        reply = await net.request(ENDPOINTS, "POST", CRUX_API, json=body, headers=headers)
    except net.ApiError as exc:
        if exc.status == 404:
            scope = "page" if params.page else "site"
            return ok([], thin=True, reason=f"Chrome has no field data for this {scope}.")
        raise
    record = reply["record"]
    rows = []
    for metric, item in record.get("metrics", {}).items():
        p75 = (item.get("percentiles") or {}).get("p75")
        if p75 is not None:
            value = float(p75) if isinstance(p75, str) else p75
            rows.append({"metric": metric, "p75": value, "rating": _rating(metric, value)})
    return ok(rows, thin=not rows, window=record.get("collectionPeriod"))


SOURCES = {"page_speed": page_speed, "chrome_field_data": chrome_field_data}
ACCEPTS = {name: frozenset({"page"}) for name in SOURCES}

STRATEGIES = ("MOBILE", "DESKTOP")
PSI_CATEGORIES = ("ACCESSIBILITY", "BEST_PRACTICES", "PERFORMANCE", "SEO")
FORM_FACTORS = ("PHONE", "DESKTOP", "TABLET")
CRUX_HISTORY_API = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"
MAX_PERIODS = 40


async def _psi(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    url = page_url(site.host, request.get("url", "/"), "url")
    query: list[tuple[str, str]] = [("url", url)]
    if "strategy" in request:
        one_of("strategy", request["strategy"], STRATEGIES)
        query.append(("strategy", request["strategy"]))
    categories = request.get("category", [])
    if not isinstance(categories, list):
        raise BadInput("category must be a list.")
    for name in categories:
        one_of("Each category", name, PSI_CATEGORIES)
        query.append(("category", name))
    if "locale" in request:
        query.append(("locale", str(request["locale"])))
    headers = _key_header(site.optional_secret("google_api_key"))
    reply = await net.request(
        ENDPOINTS, "GET", PSI_API, params=query, headers=headers, explain=True
    )
    lighthouse = reply.get("lighthouseResult", {})
    rows = [
        {
            "audit": name,
            "title": item.get("title"),
            "score": item.get("score"),
            "value": item.get("numericValue"),
            "display": item.get("displayValue"),
        }
        for name, item in lighthouse.get("audits", {}).items()
    ]
    scores = {
        name: round(item["score"] * 100)
        for name, item in lighthouse.get("categories", {}).items()
        if item.get("score") is not None
    }
    return answer(
        rows,
        more_rows="no",
        applied=dict(query),
        scores=scores,
        lab_run=lighthouse.get("fetchTime"),
        field_data=reply.get("loadingExperience"),
    )


def _crux_reader(endpoint: str, periods: str) -> Run:
    async def run(site: Site, request: dict[str, Any]) -> dict[str, Any]:
        if "origin" in request and "url" in request:
            raise BadInput("Give origin or url, not both.")
        body = dict(request)
        if "url" in request:
            body["url"] = page_url(site.host, request["url"], "url")
        elif request.get("origin", f"https://{site.host}") != f"https://{site.host}":
            raise BadInput(f"origin must be https://{site.host}.")
        else:
            body["origin"] = f"https://{site.host}"
        if "formFactor" in request:
            one_of("formFactor", request["formFactor"], FORM_FACTORS)
        if "collectionPeriodCount" in request:
            whole("collectionPeriodCount", request["collectionPeriodCount"], 1, MAX_PERIODS)
        headers = _key_header(site.secret("google_api_key"))
        try:
            reply = await net.request(
                ENDPOINTS, "POST", endpoint, json=body, headers=headers, explain=True
            )
        except net.ApiError as exc:
            if exc.status == 404:
                reason = "Chrome has no field data for this request."
                return answer([], more_rows="no", reason=reason, applied=body)
            raise
        record = reply.get("record", {})
        rows = [{"metric": name, **item} for name, item in record.get("metrics", {}).items()]
        extra = {periods: record[periods]} if periods in record else {}
        return answer(rows, more_rows="no", applied=body, key=record.get("key"), **extra)

    return run


CRUX_FIELDS = frozenset({"origin", "url", "formFactor", "metrics"})
APIS = {
    "pagespeed": Api(
        title="PageSpeed Insights",
        docs="https://developers.google.com/speed/docs/insights/rest/v5/pagespeedapi/runpagespeed",
        methods={
            "run": Method(
                _psi,
                "A live Lighthouse run of 1 page.",
                frozenset({"url", "strategy", "category", "locale"}),
            )
        },
        options={"strategies": list(STRATEGIES), "categories": list(PSI_CATEGORIES)},
        gaps=(
            "Old runs: each call is a new live test.",
            "The detail tables and screenshots of an audit: the server keeps the summary of "
            "each audit, so that the result fits in a model context.",
        ),
    ),
    "crux": Api(
        title="Chrome UX Report",
        docs="https://developer.chrome.com/docs/crux/api",
        methods={
            "record": Method(
                _crux_reader(CRUX_API, "collectionPeriod"),
                "The field data of the last 28 days.",
                CRUX_FIELDS,
            ),
            "history": Method(
                _crux_reader(CRUX_HISTORY_API, "collectionPeriods"),
                "The field data over time, 1 to 40 periods.",
                CRUX_FIELDS | {"collectionPeriodCount"},
            ),
        },
        options={"form_factors": list(FORM_FACTORS), "max_periods": MAX_PERIODS},
        gaps=(
            "Daily field data: the API gives only 28-day windows.",
            "Pages with too little Chrome traffic: the API answers 404.",
        ),
    ),
}
