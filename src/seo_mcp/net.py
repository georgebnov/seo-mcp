"""The only module that opens a connection. It refuses each call that is not listed."""

import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx2

REQUEST_TIMEOUT_SECONDS = 90

# Tests replace this. Nothing else mocks the network.
transport: httpx2.AsyncBaseTransport | None = None


class Endpoint(NamedTuple):
    method: str
    host: str
    path: str


class NotAllowed(Exception):
    """A provider asked for a call outside its ENDPOINTS list."""


class ApiError(Exception):
    """A failed call. It holds a status only: a URL or a body can hold a key."""

    def __init__(self, status: int | None) -> None:
        super().__init__(f"HTTP {status}" if status else "The connection failed.")
        self.status = status


def _check(allowed: tuple[Endpoint, ...], method: str, url: str) -> None:
    parts = urlsplit(url)
    listed = (
        parts.scheme == "https"
        and parts.netloc == parts.hostname
        and any(
            endpoint.method == method
            and endpoint.host == parts.hostname
            and re.fullmatch(endpoint.path, parts.path)
            for endpoint in allowed
        )
    )
    if not listed:
        raise NotAllowed(f"{method} to {parts.hostname} is not in the ENDPOINTS list.")


async def request(allowed: tuple[Endpoint, ...], method: str, url: str, **kwargs: Any) -> Any:
    _check(allowed, method, url)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            response = await client.request(method, url, **kwargs)
    except httpx2.HTTPError:
        raise ApiError(None) from None
    if not response.is_success:
        raise ApiError(response.status_code)
    try:
        return response.json()
    except ValueError:
        raise ApiError(response.status_code) from None
