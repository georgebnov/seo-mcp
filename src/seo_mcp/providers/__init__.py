"""To add a provider: write 1 file with ENDPOINTS, SOURCES, and PROVIDER_OF, then list it here.

Each source and each method runs behind the connection gate: a provider that the site is not
connected to never runs, so it reads no secret, no saved answer, and no ledger.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from seo_mcp import connection
from seo_mcp.api import Api
from seo_mcp.config import Site
from seo_mcp.net import Endpoint
from seo_mcp.params import Params
from seo_mcp.providers import _google, bing, clarity, ga4, gsc, pagespeed

Source = Callable[[Site, Params], Awaitable[dict[str, Any]]]

MODULES: tuple[Any, ...] = (_google, gsc, ga4, pagespeed, bing, clarity)


def merge(modules: tuple[Any, ...], table: str) -> dict[str, Any]:
    """Join 1 table of each module. A name in 2 modules is an error, never an overwrite."""
    merged: dict[str, Any] = {}
    for module in modules:
        for name, value in getattr(module, table).items():
            if name in merged:
                raise ValueError(f"{table} has {name} in 2 provider modules.")
            merged[name] = value
    return merged


def _gated[T](provider: str, run: Callable[[Site, T], Awaitable[dict[str, Any]]]) -> Any:
    if provider not in connection.NEEDS:
        raise ValueError(f"{provider} has no entry in connection.NEEDS.")

    async def gated(site: Site, asked: T) -> dict[str, Any]:
        # The check can run `security`, so it runs off the event loop.
        await asyncio.to_thread(connection.require, site, provider)
        return await run(site, asked)

    return gated


PROVIDER_OF: dict[str, str] = merge(MODULES, "PROVIDER_OF")
if set(PROVIDER_OF) != set(merge(MODULES, "SOURCES")):
    raise ValueError("Each source needs 1 provider in the PROVIDER_OF of its module.")
SOURCES: dict[str, Source] = {
    name: _gated(PROVIDER_OF[name], run) for name, run in merge(MODULES, "SOURCES").items()
}
ACCEPTS: dict[str, frozenset[str]] = merge(MODULES, "ACCEPTS")
APIS: dict[str, Api] = {
    name: entry._replace(
        methods={
            method: item._replace(run=_gated(name, item.run))
            for method, item in entry.methods.items()
        }
    )
    for name, entry in merge(MODULES, "APIS").items()
}
ENDPOINTS: tuple[Endpoint, ...] = tuple(e for m in MODULES for e in m.ENDPOINTS)
