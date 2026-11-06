"""The only module that opens a connection. It refuses each call that is not listed."""

import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx2

REQUEST_TIMEOUT_SECONDS = 90

BAD_SHAPE = "The answer does not have the shape that this server reads, so it is not saved."
NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)

# Tests replace this. Nothing else mocks the network.
transport: httpx2.AsyncBaseTransport | None = None


class Endpoint(NamedTuple):
    method: str
    host: str
    path: str
    metered: str = ""


class Receipt:
    """Proof that a ledger counted calls. Each metered call uses 1 of them."""

    def __init__(self, ledger: str, calls: int, day: str) -> None:
        self.ledger = ledger
        self.calls = calls
        self.day = day


class NotAllowed(Exception):
    """A provider asked for a call outside its ENDPOINTS list."""


class ApiError(Exception):
    """A failed call. It holds a status, and the API's own message only when a caller asks.

    sent is False only when no byte can have reached the API. After a read timeout the API can
    have counted the request, so that call stays possibly sent.
    """

    def __init__(
        self, status: int | None, detail: str | None = None, *, sent: bool = True, note: str = ""
    ) -> None:
        super().__init__(f"HTTP {status}" if status else "The connection failed.")
        self.status = status
        self.detail = detail
        self.sent = sent
        self.note = note


def _check(allowed: tuple[Endpoint, ...], method: str, url: str) -> Endpoint:
    parts = urlsplit(url)
    secure = parts.scheme == "https" and parts.netloc == parts.hostname
    listed = [
        endpoint
        for endpoint in allowed
        if endpoint.method == method
        and endpoint.host == parts.hostname
        and re.fullmatch(endpoint.path, parts.path)
    ]
    if not secure or not listed:
        raise NotAllowed(f"{method} to {parts.hostname} is not in the ENDPOINTS list.")
    return listed[0]


def _pay(endpoint: Endpoint, spent: Receipt | None) -> None:
    if not endpoint.metered:
        return
    if spent is None or spent.ledger != endpoint.metered or spent.calls < 1:
        raise NotAllowed(f"A call to {endpoint.host} needs a receipt of {endpoint.metered}.")
    spent.calls -= 1


def _google_message(response: httpx2.Response) -> str | None:
    """Read the 1 text field of a Google validation error. All other text stays out."""
    try:
        error = response.json()["error"]
        message = error["message"] if isinstance(error, dict) else None
    except (ValueError, KeyError, TypeError):
        return None
    return message if isinstance(message, str) else None


async def request(
    allowed: tuple[Endpoint, ...],
    method: str,
    url: str,
    *,
    explain: bool = False,
    spent: Receipt | None = None,
    **kwargs: Any,
) -> Any:
    _pay(_check(allowed, method, url), spent)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            response = await client.request(method, url, **kwargs)
    except httpx2.HTTPError as exc:
        raise ApiError(None, sent=not isinstance(exc, NOT_SENT)) from None
    if not response.is_success:
        detail = _google_message(response) if explain and response.status_code == 400 else None
        raise ApiError(response.status_code, detail)
    try:
        return response.json()
    except ValueError:
        raise ApiError(response.status_code, BAD_SHAPE) from None
