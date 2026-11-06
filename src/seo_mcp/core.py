"""Run the sources of a site at the same time and build the 1 answer."""

import asyncio
from typing import Any

from seo_mcp import config, net, providers
from seo_mcp.config import ConfigError, Site
from seo_mcp.results import NotConnected, failed, ok
from seo_mcp.secrets import safe_reason, scrub

SOURCE_TIMEOUT_SECONDS: float = 100
STATUS_HINTS = {
    401: "The credentials were refused.",
    403: "The account has no access to this property, or the API is off.",
    404: "The property or the URL does not exist.",
    429: "The API rate limit is reached.",
}


async def _run(name: str, site: Site) -> dict[str, Any]:
    # A reason is fixed text. Exception text can hold a URL, and a URL can hold a key.
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            return await providers.SOURCES[name](site)
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except TimeoutError:
        return failed("error", f"The source timed out after {SOURCE_TIMEOUT_SECONDS:g} seconds.")
    except net.ApiError as exc:
        hint = STATUS_HINTS.get(exc.status or 0, "")
        return failed("error", f"{exc} from the API. {hint}".strip())
    except Exception as exc:  # noqa: BLE001
        return failed("error", f"The source failed with {type(exc).__name__}.")


def _top_status(results: dict[str, dict[str, Any]]) -> str:
    states = {result["status"] for result in results.values()}
    if "ok" in states:
        return "ok"
    return "not_connected" if states == {"not_connected"} else "error"


async def get_seo_data(site: str = "", sources: list[str] | None = None) -> dict[str, Any]:
    try:
        entry = config.resolve(site)
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except ConfigError as exc:
        return failed("error", safe_reason(str(exc)))
    names = list(providers.SOURCES) if sources is None else sources
    unknown = sorted(set(names) - set(providers.SOURCES))
    if unknown or not names:
        known = ", ".join(providers.SOURCES)
        return failed("error", safe_reason(f"Unknown sources: {unknown}. Known sources: {known}."))
    found = await asyncio.gather(*(_run(name, entry) for name in names))
    results = dict(zip(names, found, strict=True))
    thin = [name for name, result in results.items() if result["thin_data"]]
    states = [result["status"] for result in results.values()]
    return scrub(
        {
            "status": _top_status(results),
            "reason": None,
            "site": entry.host,
            "thin_data": bool(thin),
            "thin_sources": thin,
            "counts": {state: states.count(state) for state in ("ok", "not_connected", "error")},
            "sources": results,
        }
    )


def list_sites() -> dict[str, Any]:
    try:
        sites = config.load()
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except ConfigError as exc:
        return failed("error", safe_reason(str(exc)))
    rows = [
        {
            "host": site.host,
            "repo_path": str(site.repo_path),
            "fields": sorted(site.fields),
            "secrets": sorted(site.secrets),
        }
        for site in sites
    ]
    return scrub(ok(rows, thin=not rows, limit=len(rows)))
