"""To add a provider: write 1 file with ENDPOINTS and SOURCES, then list it here."""

from collections.abc import Awaitable, Callable
from typing import Any

from seo_mcp.config import Site
from seo_mcp.net import Endpoint
from seo_mcp.providers import bing

Source = Callable[[Site], Awaitable[dict[str, Any]]]

MODULES: tuple[Any, ...] = (bing,)

SOURCES: dict[str, Source] = {name: fn for m in MODULES for name, fn in m.SOURCES.items()}
ENDPOINTS: tuple[Endpoint, ...] = tuple(e for m in MODULES for e in m.ENDPOINTS)
