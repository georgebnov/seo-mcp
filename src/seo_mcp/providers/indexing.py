"""The Google Indexing API: the 1 write of this server, for the submit_urls tool only.

It sends URL_UPDATED for each URL of a site that opted in (the field submit_urls is "on").
It has no source and no query_api method, so no read path can reach the publish endpoint.
"""

import asyncio
import re
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from seo_mcp import net, quota, shared
from seo_mcp.api import Api, Quota
from seo_mcp.config import ON, Site
from seo_mcp.providers import _google
from seo_mcp.results import Refused, ok

HOST = "indexing.googleapis.com"
PUBLISH_API = f"https://{HOST}/v3/urlNotifications:publish"
SCOPE = "https://www.googleapis.com/auth/indexing"
LEDGER = quota.Ledger("indexing-publish-calls", "Cloud project", 200, quota.PACIFIC, True)
ENDPOINTS = (net.Endpoint("POST", HOST, r"/v3/urlNotifications:publish", LEDGER.name),)
SOURCES: dict[str, Any] = {}
ACCEPTS: dict[str, frozenset[str]] = {}
PROVIDER_OF: dict[str, str] = {}
DOCS = "https://developers.google.com/search/apis/indexing-api/v3/using-api"
QUOTA_DOCS = "https://developers.google.com/search/apis/indexing-api/v3/quota-pricing"
# A saved submit serves for as long as shared/ keeps it, so a repeat of 1 URL inside that time
# sends nothing and spends nothing. After it, the same URL goes out again and spends 1.
RESUBMIT_SECONDS = shared.PRUNE_SECONDS
# 10 calls at the same time keep 100 URLs far inside the source timeout.
PARALLEL = 10
MAX_URL = 2000

OFF = "submit_urls is off for this site, so the server sends nothing."
SUBMITTED = "Google took the notification. It does not promise a crawl or an index."
SAVED = (
    "This URL went to Google less than 1 hour ago, so the server did not send it again and "
    "spent no call. cached_at is the time of that answer."
)
UNREAD = (
    "Google answered with a success code, so it probably took the URL, but the server could "
    "not read the answer. It counts as spent. The same URL does not go out again for 1 hour."
)
UNREAD_SAVED = (
    "This URL went to Google less than 1 hour ago, with an answer that the server could not "
    "read, so the server did not send it again and spent no call."
)
MAYBE_SENT = (
    "The call failed after the request went out, so Google can have it. It counts as spent. "
    "The same URL does not go out again for 60 seconds."
)
NOT_SENT = "The connection failed before the request went out. It counts as spent."
DAY_ENDED = "The quota day ended between the count and the send, so the server did not send it."
REASONS = {
    400: "Google refused the request (HTTP 400): the URL is not valid for the Indexing API.",
    401: "Google refused the credentials (HTTP 401).",
    403: (
        "Google refused the call (HTTP 403): the service account is not an Owner of the "
        "Search Console property, or the Indexing API is off in its Cloud project."
    ),
    429: "Google refused the call (HTTP 429): the quota or the rate limit is reached.",
}
HIDDEN_URL = "(not shown: the URL holds a user name or a password)"
NOT_HTTP = "The URL is not an absolute http or https URL."
HAS_USER = "The URL holds a user name or a password."
ODD_TEXT = "The URL holds a space, a control character, a backslash, or text that is not ASCII."
TOO_LONG = f"The URL is longer than {MAX_URL} characters."
DOT_PATH = "The path holds . or .. parts, so it can name a page outside the property."
FRAGMENT = "The URL holds a # fragment: Google reads a page with no fragment."
PORT = "The URL names a port that the property does not have."
OUTSIDE = "The URL is not in the Search Console property of this site ({prop})."
NO_PROPERTY = "The gsc_property of this site is not a domain or a URL-prefix property."
ASCII_URL = re.compile(r"[!-~]+")


def _host_ok(host: str) -> bool:
    return bool(host) and not host.endswith(".") and ".." not in host


def _dot_path(path: str) -> bool:
    return any(part in {".", ".."} for part in unquote(path).split("/"))


def problem(prop: str, url: str) -> str | None:
    """Give the fixed reason that url is not in the property, or None. It sends nothing.

    A domain property sc-domain:example.com holds http and https URLs of example.com and of
    each subdomain. A URL-prefix property holds the URLs that start with its scheme, host,
    port, and path. An unclear URL is refused, never rewritten.
    """
    if len(url) > MAX_URL:
        return TOO_LONG
    if not ASCII_URL.fullmatch(url) or "\\" in url:
        return ODD_TEXT
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return NOT_HTTP
    if "@" in parts.netloc:
        return HAS_USER
    if "#" in url:
        return FRAGMENT
    try:
        port = parts.port
    except ValueError:
        return NOT_HTTP
    host = (parts.hostname or "").lower()
    if not _host_ok(host):
        return NOT_HTTP
    if _dot_path(parts.path):
        return DOT_PATH
    if prop.startswith("sc-domain:"):
        domain = prop.removeprefix("sc-domain:").lower()
        if port is not None:
            return PORT
        inside = host == domain or host.endswith(f".{domain}")
        return None if inside else OUTSIDE.format(prop=prop)
    base = urlsplit(prop)
    if base.scheme not in ("http", "https") or not base.hostname:
        return NO_PROPERTY
    if port != base.port:
        return PORT
    same = parts.scheme == base.scheme and host == base.hostname.lower()
    inside = same and (parts.path or "/").startswith(base.path or "/")
    return None if inside else OUTSIDE.format(prop=prop)


def _notify_time(reply: Any) -> str | None:
    """The 1 field that a result shows. It must parse as a time, so it holds no other text."""
    try:
        value = reply["urlNotificationMetadata"]["latestUpdate"]["notifyTime"]
        datetime.fromisoformat(value)
    except (KeyError, TypeError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _echoed(reply: Any, url: str) -> bool:
    """Google took the URL when its answer names the same URL. latestUpdate can be missing."""
    meta = reply.get("urlNotificationMetadata") if isinstance(reply, dict) else None
    return isinstance(meta, dict) and meta.get("url") == url


def _any(_: Any) -> bool:
    """Each success answer is saved, also one that the server cannot read: Google can have it."""
    return True


def _failure(exc: net.ApiError) -> str:
    if exc.note:
        return exc.note
    if exc.status is None:
        return MAYBE_SENT if exc.sent else NOT_SENT
    return REASONS.get(exc.status, f"Google answered HTTP {exc.status}.")


def _refused(url: str, reason: str) -> dict[str, Any]:
    shown = HIDDEN_URL if reason == HAS_USER else url
    return {"url": shown, "status": "refused", "reason": reason}


async def _one(
    site: Site, url: str, headers: dict[str, str], key: str, gate: asyncio.Semaphore
) -> dict[str, Any]:
    """Send 1 URL. Each URL has its own saved answer, so a repeat of 1 URL spends nothing."""
    async with gate:
        shared.begin()
        path = shared.entry(site, "submit_urls", url, auth=_google.principal(site))

        async def prepare() -> net.Receipt:
            return await quota.spend(LEDGER, key)

        async def send(spent: net.Receipt) -> Any:
            # A receipt of the old day must not open a call of the new day.
            if spent.day != quota.today(LEDGER):
                raise net.ApiError(None, sent=False, note=DAY_ENDED)
            body = {"url": url, "type": "URL_UPDATED"}
            try:
                return await net.request(
                    ENDPOINTS, "POST", PUBLISH_API, json=body, headers=headers, spent=spent
                )
            except net.ApiError as exc:
                # A success with a body that is not JSON: Google can have the URL.
                if exc.status is not None and 200 <= exc.status < 300:
                    return {}
                raise

        try:
            reply = await shared.once(
                path, send, prepare=prepare, good=_any, until=shared.after(RESUBMIT_SECONDS)
            )
        except Refused as exc:
            return {**_refused(url, str(exc)), **shared.shown()}
        except net.ApiError as exc:
            return {"url": url, "status": "failed", "reason": _failure(exc), **shared.shown()}
        seen = shared.shown()
        cached = bool(seen.get("from_cache"))
        if _echoed(reply, url):
            status, reason = "submitted", SAVED if cached else SUBMITTED
        else:
            status, reason = "unconfirmed", UNREAD_SAVED if cached else UNREAD
        row = {"url": url, "status": status, "notify_time": _notify_time(reply)}
        return {**row, "reason": reason, **seen}


async def submit(site: Site, urls: list[str]) -> dict[str, Any]:
    if site.field("submit_urls") != ON:
        raise Refused(OFF)
    prop = site.field("gsc_property")
    wanted = list(dict.fromkeys(urls))
    found = {url: problem(prop, url) for url in wanted}
    good = [url for url, reason in found.items() if reason is None]
    rows: dict[str, dict[str, Any]] = {
        url: _refused(url, reason) for url, reason in found.items() if reason
    }
    if good:
        key = _google.project(site)
        quota.show(lambda: quota.view(LEDGER, key))
        headers = {"Authorization": f"Bearer {await _google.token(site, SCOPE)}"}
        gate = asyncio.Semaphore(PARALLEL)
        sent = await asyncio.gather(*(_one(site, url, headers, key, gate) for url in good))
        rows |= dict(zip(good, sent, strict=True))
    ordered = [rows[url] for url in wanted]
    counts = {
        state: sum(row["status"] == state for row in ordered)
        for state in ("submitted", "unconfirmed", "refused", "failed")
    }
    return ok(ordered, thin=not counts["submitted"], limit=len(ordered), counts=counts)


APIS = {
    "indexing": Api(
        title="Google Indexing API",
        summary="No method here. Only the submit_urls tool writes to it, for opted-in sites.",
        docs=DOCS,
        quota=Quota(
            QUOTA_DOCS,
            (
                f"Publish: {LEDGER.limit} requests each day for each Google Cloud project. "
                "Each URL counts 1, also in a batch.",
                "The day resets at midnight Pacific time. The server counts the same day.",
                "The server counts only the publish requests of submit_urls. The count of the "
                "project shows in each submit_urls answer: the key file names the project, and "
                "list_sites reads no secret.",
            ),
        ),
        methods={},
        options={},
        gaps=(
            "Google documents this API only for pages with JobPosting or BroadcastEvent "
            "(in a VideoObject) structured data.",
            "A notification does not promise a crawl or an index.",
            "URL_DELETED and the metadata read: this server does not send them.",
        ),
    )
}
