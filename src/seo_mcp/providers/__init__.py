"""To add a provider: write 1 file with ENDPOINTS, SOURCES, ACCEPTS, and APIS, then list it here."""

from collections.abc import Awaitable, Callable
from typing import Any

from seo_mcp.api import Api
from seo_mcp.config import Site
from seo_mcp.net import Endpoint
from seo_mcp.params import Params
from seo_mcp.providers import _google, bing, clarity, ga4, gsc, pagespeed

Source = Callable[[Site, Params], Awaitable[dict[str, Any]]]

MODULES: tuple[Any, ...] = (_google, gsc, ga4, pagespeed, bing, clarity)

SOURCES: dict[str, Source] = {name: fn for m in MODULES for name, fn in m.SOURCES.items()}
ACCEPTS: dict[str, frozenset[str]] = {
    name: names for m in MODULES for name, names in m.ACCEPTS.items()
}
APIS: dict[str, Api] = {name: entry for m in MODULES for name, entry in m.APIS.items()}
ENDPOINTS: tuple[Endpoint, ...] = tuple(e for m in MODULES for e in m.ENDPOINTS)
