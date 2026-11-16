"""Google service account sign-in, shared by the Search Console and GA4 providers."""

import json
import re
import time
from datetime import date, timedelta
from typing import Any

from google.auth import crypt, jwt

from seo_mcp import net, secrets, shared
from seo_mcp.config import Site
from seo_mcp.results import NotConnected

OAUTH_URL = "https://oauth2.googleapis.com/token"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
ENDPOINTS = (net.Endpoint("POST", "oauth2.googleapis.com", r"/token"),)
SOURCES: dict[str, Any] = {}
ACCEPTS: dict[str, frozenset[str]] = {}
PROVIDER_OF: dict[str, str] = {}
APIS: dict[str, Any] = {}
TOKEN_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 60
UNKNOWN_REASON = "No day of the range is complete, so the total is unknown."

# Google names a key of a user-made service account NAME@PROJECT.iam.gserviceaccount.com.
PROJECT_EMAIL = re.compile(r"[^@]+@([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com")
NO_PROJECT = (
    "The client_email of the Google service account does not name a Cloud project "
    "(NAME@PROJECT.iam.gserviceaccount.com), so the server cannot count its project quota."
)

_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def described(
    start: str,
    end: str,
    last: str,
    source: str,
    how: str,
    zone: str,
    partial: dict[str, str] | None,
    **extra: str,
) -> dict[str, Any]:
    """Tell a real 0 from a partial day. last never comes from the rows of the answer."""
    return {
        "start_date": start,
        "end_date": end,
        "last_complete_day": last,
        "last_complete_day_source": source,
        "last_complete_day_how": how,
        "time_zone": zone,
        **extra,
        "partial_days": partial,
    }


def coverage(
    start: date, end: date, last: date, source: str, how: str, zone: str, **extra: str
) -> dict[str, Any]:
    first = max(start, last + timedelta(days=1))
    partial = {"from": first.isoformat(), "to": end.isoformat()} if end > last else None
    dates = (start.isoformat(), end.isoformat(), last.isoformat())
    return described(*dates, source, how, zone, partial, **extra)


def _account(site: Site) -> dict[str, str]:
    try:
        account: dict[str, str] = json.loads(site.secret("google_service_account"))
        for name in ("private_key", "private_key_id", "client_email"):
            secrets.remember(account[name])
    except (ValueError, KeyError, TypeError):
        raise NotConnected("The Google service account file is not valid.") from None
    return account


def principal(site: Site) -> shared.Identity:
    """The account that reads, by its email. The private key stays out of the identity.

    A rotated key of 1 account has the same access, and a key that does not sign fails at the
    token step, before any saved answer or failure.
    """
    return shared.identity("google_service_account", _account(site)["client_email"])


def project(site: Site) -> str:
    """The Cloud project of the account, from the email that signs each grant.

    A project_id field of the file is only a label: 2 files with 1 key could name 2 projects.
    The email is the issuer of the grant, so it is the account that Google counts.
    """
    match = PROJECT_EMAIL.fullmatch(_account(site)["client_email"])
    if not match:
        raise NotConnected(NO_PROJECT)
    return match.group(1)


async def token(site: Site, scope: str) -> str:
    account = _account(site)
    cached = _tokens.get((account["client_email"], scope))
    if cached and cached[1] > time.time() + REFRESH_MARGIN_SECONDS:
        return cached[0]
    now = int(time.time())
    try:
        signer = crypt.RSASigner.from_service_account_info(account)
    except ValueError:
        raise NotConnected("The Google service account key is not valid.") from None
    claims = {
        "iss": account["client_email"],
        "scope": scope,
        "aud": OAUTH_URL,
        "iat": now,
        "exp": now + TOKEN_SECONDS,
    }
    grant = jwt.encode(signer, claims).decode()
    secrets.remember(grant)
    body = await net.request(
        ENDPOINTS,
        "POST",
        OAUTH_URL,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": grant},
    )
    value: str = body["access_token"]
    secrets.remember(value)
    _tokens[(account["client_email"], scope)] = (value, now + TOKEN_SECONDS)
    return value
