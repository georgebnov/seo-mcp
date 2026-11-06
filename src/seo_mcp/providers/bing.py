"""Bing Webmaster Tools: inbound links."""

import json
import re
from datetime import UTC, datetime
from typing import Any

from seo_mcp import net
from seo_mcp.api import Api, Method, Run, answer, as_rows, whole
from seo_mcp.config import Site
from seo_mcp.params import MAX_TEXT, BadInput, Params, page_url
from seo_mcp.results import ok

API = "https://ssl.bing.com/webmaster/api.svc/json"
URL, TEXT, INDEX = "url", "text", "index"
# Only site reads whose arguments the reference shows. Each name becomes 1 endpoint.
METHODS: dict[str, dict[str, str]] = {
    "GetQueryStats": {},
    "GetPageStats": {},
    "GetRankAndTrafficStats": {},
    "GetCrawlStats": {},
    "GetCrawlIssues": {},
    "GetFeeds": {},
    "GetBlockedUrls": {},
    "GetConnectedPages": {},
    "GetQueryParameters": {},
    "GetCountryRegionSettings": {},
    "GetPageQueryStats": {"page": URL},
    "GetQueryPageStats": {"query": TEXT},
    "GetQueryPageDetailStats": {"query": TEXT, "page": URL},
    "GetLinkCounts": {"page": INDEX},
    "GetUrlLinks": {"link": URL, "page": INDEX},
    "GetUrlInfo": {"url": URL},
    "GetUrlTrafficInfo": {"url": URL},
}
ENDPOINTS = tuple(
    net.Endpoint("GET", "ssl.bing.com", rf"/webmaster/api\.svc/json/{name}") for name in METHODS
)
THIN_INBOUND_LINKS = 10
MAX_PAGE_INDEX = 32767
WCF_DATE = re.compile(r"/Date\((-?\d+)[+-]\d{4}\)/")
LIST_KEYS = ("Links", "Details")


async def bing_links(site: Site, params: Params) -> dict[str, Any]:
    site_url = site.field("bing_site_url")
    # The Bing API takes its key only in the query string.
    query = {"siteUrl": site_url, "page": 0, "apikey": site.secret("bing_api_key")}
    body = await net.request(ENDPOINTS, "GET", f"{API}/GetLinkCounts", params=query)
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
ACCEPTS = {"bing_links": frozenset[str]()}


def _argument(site: Site, name: str, kind: str, value: Any) -> Any:
    if kind == INDEX:
        return whole(name, value, 0, MAX_PAGE_INDEX)
    if kind == URL:
        # The API answers HTTP 400 to a URL in quotes.
        return page_url(site.host, value, name)
    if isinstance(value, str) and 1 <= len(value) <= MAX_TEXT:
        # The JSON form of this API reads a text argument as a JSON string, with its quotes.
        return json.dumps(value)
    raise BadInput(f"{name} must be text of 1 to {MAX_TEXT} characters.")


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in row.items():
        found = WCF_DATE.fullmatch(value) if isinstance(value, str) else None
        if found:
            value = datetime.fromtimestamp(int(found[1]) / 1000, UTC).date().isoformat()
        if key != "__type":
            clean[key] = value
    return clean


def _reader(name: str) -> Run:
    async def run(site: Site, request: dict[str, Any]) -> dict[str, Any]:
        arguments = METHODS[name]
        query: dict[str, Any] = {"siteUrl": site.field("bing_site_url")}
        given = {key: request.get(key, 0) for key, kind in arguments.items() if kind == INDEX}
        given |= request
        for key, value in given.items():
            query[key] = _argument(site, key, arguments[key], value)
        query["apikey"] = site.secret("bing_api_key")
        reply = await net.request(ENDPOINTS, "GET", f"{API}/{name}", params=query)
        data = reply["d"]
        pages = data.get("TotalPages") if isinstance(data, dict) else None
        listed = next(
            (data[key] for key in LIST_KEYS if isinstance(data, dict) and key in data), None
        )
        rows = [_clean(row) for row in as_rows(data if listed is None else listed or [])]
        at = request.get("page", 0) if arguments.get("page") == INDEX else 0
        more = "yes" if pages and at + 1 < pages else "no"
        return answer(rows, more_rows=more, applied=request, total_pages=pages)

    return run


APIS = {
    "bing": Api(
        title="Bing Webmaster Tools",
        docs="https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi",
        methods={
            name: Method(
                _reader(name),
                f"The official method {name}.",
                frozenset(arguments),
                frozenset(key for key, kind in arguments.items() if kind != INDEX),
            )
            for name, arguments in METHODS.items()
        },
        options={"arguments": METHODS, "page": "A URL for page, link, or url, or a page index."},
        gaps=(
            "Keyword research (GetKeyword, GetKeywordStats, GetRelatedKeywords): the reference "
            "shows no JSON request form, so this server does not call them.",
            "A date range: the Bing traffic methods take none, and Bing updates them each week.",
        ),
    )
}
