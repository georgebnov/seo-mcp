"""Bing Webmaster Tools: inbound links."""

from typing import Any

from seo_mcp import net
from seo_mcp.config import Site
from seo_mcp.results import ok

API = "https://ssl.bing.com/webmaster/api.svc/json"
ENDPOINTS = (net.Endpoint("GET", "ssl.bing.com", r"/webmaster/api\.svc/json/GetLinkCounts"),)
THIN_INBOUND_LINKS = 10


async def bing_links(site: Site) -> dict[str, Any]:
    site_url = site.field("bing_site_url")
    # The Bing API takes its key only in the query string.
    params = {"siteUrl": site_url, "page": 0, "apikey": site.secret("bing_api_key")}
    body = await net.request(ENDPOINTS, "GET", f"{API}/GetLinkCounts", params=params)
    data = body["d"]
    rows = [{"url": row["Url"], "inbound_links": row["Count"]} for row in data.get("Links") or []]
    rows.sort(key=lambda row: row["inbound_links"], reverse=True)
    total = sum(row["inbound_links"] for row in rows)
    pages = data.get("TotalPages") or 1
    reason = f"This is page 1 of {pages} of the link list." if pages > 1 else None
    if not rows:
        reason = "Bing has no inbound links on record for this site."
    return ok(rows, thin=total < THIN_INBOUND_LINKS, reason=reason, totals={"inbound_links": total})


SOURCES = {"bing_links": bing_links}
