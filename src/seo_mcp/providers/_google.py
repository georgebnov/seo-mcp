"""Google service account sign-in, shared by the Search Console and GA4 providers."""

import json
import time

from google.auth import crypt, jwt

from seo_mcp import net, secrets
from seo_mcp.config import Site
from seo_mcp.results import NotConnected

OAUTH_URL = "https://oauth2.googleapis.com/token"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
AUTH_ENDPOINTS = (net.Endpoint("POST", "oauth2.googleapis.com", r"/token"),)
TOKEN_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 60

_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def _account(site: Site) -> dict[str, str]:
    try:
        account: dict[str, str] = json.loads(site.secret("google_service_account"))
        for name in ("private_key", "private_key_id", "client_email"):
            secrets.remember(account[name])
    except (ValueError, KeyError, TypeError):
        raise NotConnected("The Google service account file is not valid.") from None
    return account


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
        AUTH_ENDPOINTS,
        "POST",
        OAUTH_URL,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": grant},
    )
    value: str = body["access_token"]
    secrets.remember(value)
    _tokens[(account["client_email"], scope)] = (value, now + TOKEN_SECONDS)
    return value
