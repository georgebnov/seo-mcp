"""PageSpeed Insights (lab data) and the Chrome UX Report (field data)."""

from typing import Any

from seo_mcp import net
from seo_mcp.config import Site
from seo_mcp.results import ok

PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_API = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
ENDPOINTS = (
    net.Endpoint("GET", "www.googleapis.com", r"/pagespeedonline/v5/runPagespeed"),
    net.Endpoint("POST", "chromeuxreport.googleapis.com", r"/v1/records:queryRecord"),
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


async def page_speed(site: Site) -> dict[str, Any]:
    params = [("url", f"https://{site.host}/"), ("strategy", "mobile")]
    params += [("category", name) for name in CATEGORIES]
    headers = _key_header(site.optional_secret("google_api_key"))
    answer = await net.request(ENDPOINTS, "GET", PSI_API, params=params, headers=headers)
    lighthouse = answer["lighthouseResult"]
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


async def chrome_field_data(site: Site) -> dict[str, Any]:
    headers = _key_header(site.secret("google_api_key"))
    body = {"origin": f"https://{site.host}"}
    try:
        answer = await net.request(ENDPOINTS, "POST", CRUX_API, json=body, headers=headers)
    except net.ApiError as exc:
        if exc.status == 404:
            return ok([], thin=True, reason="Chrome has no field data for this site.")
        raise
    record = answer["record"]
    rows = []
    for metric, item in record.get("metrics", {}).items():
        p75 = (item.get("percentiles") or {}).get("p75")
        if p75 is not None:
            value = float(p75) if isinstance(p75, str) else p75
            rows.append({"metric": metric, "p75": value, "rating": _rating(metric, value)})
    return ok(rows, thin=not rows, window=record.get("collectionPeriod"))


SOURCES = {"page_speed": page_speed, "chrome_field_data": chrome_field_data}
