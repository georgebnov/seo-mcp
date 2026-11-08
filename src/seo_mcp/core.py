"""Run the sources of a site at the same time and build the 1 answer."""

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from seo_mcp import api as full_api
from seo_mcp import config, net, providers, quota, shared, tasks
from seo_mcp.config import ConfigError, Site
from seo_mcp.params import COMPARE_NEEDS, NONE, BadInput, Params, length, parse, today
from seo_mcp.results import NotConnected, Refused, Unsupported, failed, ok, unsupported
from seo_mcp.secrets import safe_reason, scrub

SOURCE_TIMEOUT_SECONDS: float = 100
STATUS_HINTS = {
    401: "The credentials were refused.",
    403: "The token or key is invalid or cut, the account has no access, or the API is off.",
    404: "The property or the URL does not exist.",
    429: "The API rate limit is reached.",
}


def _accepts(name: str) -> list[str]:
    accepted = providers.ACCEPTS.get(name, frozenset())
    return sorted(accepted | {key for key, need in COMPARE_NEEDS.items() if need in accepted})


def _failed(reason: str) -> dict[str, Any]:
    return {**failed("error", reason), **quota.shown(), **shared.shown()}


async def _guard(job: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    # The job runs in this task, so a quota that it names is visible here on each exit: a
    # call that failed or timed out can still have spent, and a provider cannot forget this.
    quota.forget()
    shared.begin()
    # A reason is fixed text. Exception text can hold a URL, and a URL can hold a key.
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            result = await job
            return {**result, **quota.shown(), **shared.shown()}
    except Unsupported:
        raise
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except (Refused, BadInput) as exc:
        return _failed(safe_reason(str(exc)))
    except TimeoutError:
        return _failed(f"The source timed out after {SOURCE_TIMEOUT_SECONDS:g} seconds.")
    except net.ApiError as exc:
        hint = exc.detail or STATUS_HINTS.get(exc.status or 0, "")
        said = f"{exc} from the API." if exc.status else str(exc)
        return _failed(safe_reason(" ".join(filter(None, [said, hint, exc.note]))))
    except Exception as exc:  # noqa: BLE001
        return _failed(f"The source failed with {type(exc).__name__}.")


async def _run(name: str, site: Site, params: Params) -> dict[str, Any]:
    try:
        return await _guard(providers.SOURCES[name](site, params))
    except Unsupported as exc:
        return unsupported(exc.params, safe_reason(str(exc)), _accepts(name))


def _days(result: dict[str, Any]) -> tuple[int, int] | None:
    """The fewest and the most days. A move to GA4 today has no exact length with no zone."""
    coverage = result.get("coverage")
    if not coverage:
        return None
    at = result.get("cached_at")
    day = today(datetime.fromisoformat(at) if at else None)
    return length(coverage["start_date"], coverage["end_date"], day)


def _shown(days: tuple[int, int]) -> int | list[int]:
    return days[0] if days[0] == days[1] else list(days)


def _said(days: tuple[int, int]) -> str:
    return str(days[0]) if days[0] == days[1] else f"{days[0]} to {days[1]}"


def _warn_of_unequal_ranges(result: dict[str, Any], other: dict[str, Any]) -> None:
    """A move changes 1 range only: the agent decides what to do with the other."""
    main, comparison = _days(result), _days(other)
    if "date_moved" not in result and "date_moved" not in other:
        return
    if main is None or comparison is None or main == comparison and main[0] == main[1]:
        return
    result["range_days"] = {"main": _shown(main), "comparison": _shown(comparison)}
    exact = main[0] == main[1] and comparison[0] == comparison[1]
    can = "compares" if exact else "can compare"
    lengths = f"{_said(main)} and {_said(comparison)} days"
    note = f"The 2 ranges are now {lengths}, so delta {can} unequal ranges."
    result["reason"] = " ".join(filter(None, [result["reason"], note]))


def _delta(result: dict[str, Any], other: dict[str, Any]) -> dict[str, float | None] | None:
    """Give the main value minus the comparison value, in the unit of each total.

    An unknown side (totals None) gives no delta, so that an unknown total is never a drop. A
    metric that is null on 1 side, such as a GA4 rate of 0 sessions, has a null delta.
    """
    first, second = result.get("totals"), other.get("totals")
    if first is None or second is None:
        return None
    delta: dict[str, float | None] = {}
    for name, value in first.items():
        if name not in second:
            continue
        paired = second[name]
        if value is None or paired is None:
            delta[name] = None
        elif isinstance(value, int | float) and isinstance(paired, int | float):
            delta[name] = round(value - paired, 6)
    return delta


async def _asked(
    name: str, site: Site, primary: Params, comparison: Params | None, asked: set[str]
) -> dict[str, Any]:
    accepts = _accepts(name)
    wrong = sorted(asked - set(accepts))
    if wrong:
        reason = f"{name} cannot apply {', '.join(wrong)}, so it did not run."
        return unsupported(wrong, reason, accepts)
    jobs = [_run(name, site, primary)]
    if comparison:
        jobs.append(_run(name, site, comparison))
    result, *rest = await asyncio.gather(*jobs)
    for done, params in zip([result, *rest], [primary, comparison], strict=False):
        if done["status"] == "ok" and params:
            done["applied"] = params.applied()
    if rest:
        result["comparison"] = rest[0]
        if result["status"] == rest[0]["status"] == "ok" and "totals" in result:
            result["delta"] = _delta(result, rest[0])
            _warn_of_unequal_ranges(result, rest[0])
    return result


def _top_status(results: dict[str, dict[str, Any]]) -> str:
    states = {result["status"] for result in results.values()}
    if states == {"unsupported"}:
        return "unsupported"
    states.discard("unsupported")
    if "ok" in states:
        return "ok"
    return "not_connected" if states == {"not_connected"} else "error"


async def get_seo_data(
    site: str = "",
    sources: list[str] | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    last_days: int | None = None,
    page: str | None = None,
    query: str | None = None,
    compare_start_date: str | None = None,
    compare_end_date: str | None = None,
    compare_page: str | None = None,
) -> dict[str, Any]:
    try:
        entry = config.resolve(site)
        primary, comparison, asked = parse(
            entry.host,
            start_date=start_date,
            end_date=end_date,
            last_days=last_days,
            page=page,
            query=query,
            compare_start_date=compare_start_date,
            compare_end_date=compare_end_date,
            compare_page=compare_page,
        )
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except (ConfigError, BadInput) as exc:
        return failed("error", safe_reason(str(exc)))
    names = list(providers.SOURCES) if sources is None else sources
    unknown = sorted(set(names) - set(providers.SOURCES))
    if unknown or not names:
        known = ", ".join(providers.SOURCES)
        return failed("error", safe_reason(f"Unknown sources: {unknown}. Known sources: {known}."))
    if asked:
        jobs = [_asked(name, entry, primary, comparison, asked) for name in names]
    else:
        jobs = [_run(name, entry, NONE) for name in names]
    found = await asyncio.gather(*jobs)
    results = dict(zip(names, found, strict=True))
    thin = [name for name, result in results.items() if result["thin_data"]]
    states = [result["status"] for result in results.values()]
    # A call of the old shape keeps the old fields.
    kinds = ("ok", "not_connected", "error", *(("unsupported",) if asked else ()))
    return scrub(
        {
            "status": _top_status(results),
            "reason": None,
            "site": entry.host,
            "thin_data": bool(thin),
            "thin_sources": thin,
            "counts": {state: states.count(state) for state in kinds},
            "sources": results,
        }
    )


def describe_sources() -> str:
    """Build the list for the tool description from ACCEPTS, so that it cannot drift."""
    lines = [
        f"- {name}: {', '.join(_accepts(name)) or 'no parameters'}" for name in providers.SOURCES
    ]
    return "\n".join(lines)


def describe_apis() -> str:
    """Build the list for the tool description from APIS, so that it cannot drift."""
    return "\n".join(f"- {name}: {entry.summary}" for name, entry in providers.APIS.items())


def _connected(entry: full_api.Api, site: Site) -> bool:
    return entry.quota.needs <= set(site.fields) | set(site.secrets)


def _quota(entry: full_api.Api, site: Site | None) -> dict[str, Any]:
    """Show the published limits, and the count of the site when one resolves. No network."""
    info: dict[str, Any] = {"docs": entry.quota.docs, "limits": list(entry.quota.limits)}
    if site and entry.quota.now and _connected(entry, site):
        info["now"] = entry.quota.now(site)
    return info


def _site_quota(entry: full_api.Api, site: Site) -> dict[str, Any]:
    if entry.quota.now:
        return {"tracked": True, **entry.quota.now(site)}
    return {"tracked": False, "limits": list(entry.quota.limits)}


def _target(site: str) -> Site | None:
    """Discovery works with no sites file, so a site that does not resolve is not an error."""
    try:
        return config.resolve(site)
    except (NotConnected, ConfigError):
        return None


def _catalog(site: str) -> dict[str, Any]:
    target = _target(site)
    rows = [
        {
            "api": name,
            "title": entry.title,
            "methods": sorted(entry.methods),
            "quota": _quota(entry, target),
        }
        for name, entry in providers.APIS.items()
    ]
    return ok(rows, thin=False, limit=len(rows), tasks=tasks.summaries())


def _no_task(reason: str) -> dict[str, Any]:
    return unsupported(["task"], safe_reason(reason), sorted(tasks.TASKS))


def _task(site: str, api: str, asked: str) -> dict[str, Any]:
    names = tasks.find(asked)
    if not names:
        return _no_task("No task matches this ask, and the server does not guess a call.")
    built = {name: tasks.build(name, site, api) for name in names}
    target = _target(site)
    rows = [
        {
            **row,
            "window": item["window"],
            "quota": _quota(providers.APIS[row["api"]], target),
        }
        for item in built.values()
        for row in item["rows"]
    ]
    if not rows:
        return _no_task(f"{api} has no recipe for this task.")
    return scrub(ok(rows, thin=False, limit=len(rows)))


def _options(name: str, entry: full_api.Api, site: str) -> dict[str, Any]:
    rows = [
        {
            "method": method,
            "summary": item.summary,
            "fields": sorted(item.fields),
            "required": sorted(item.required),
        }
        for method, item in entry.methods.items()
    ]
    return ok(
        rows,
        thin=False,
        limit=len(rows),
        api=name,
        docs=entry.docs,
        options=entry.options,
        not_in_the_api=list(entry.gaps),
        quota=_quota(entry, _target(site)),
    )


async def query_api(
    site: str = "",
    api: str = "",
    method: str = "",
    request: dict[str, Any] | None = None,
    task: str = "",
) -> dict[str, Any]:
    if task and (method or request is not None):
        return failed("error", "A task finds a call and sends none. Omit method and request.")
    if not api:
        return _task(site, "", task) if task else _catalog(site)
    entry = providers.APIS.get(api)
    if entry is None:
        return failed("error", safe_reason(f"Unknown api. Known: {', '.join(providers.APIS)}."))
    if task:
        return _task(site, api, task)
    if not method:
        return _options(api, entry, site)
    item = entry.methods.get(method)
    if item is None:
        return failed("error", safe_reason(f"Unknown method. Known: {', '.join(entry.methods)}."))
    body = request or {}
    try:
        full_api.check_request(method, item, body)
        target = config.resolve(site)
    except NotConnected as exc:
        return failed("not_connected", safe_reason(str(exc)))
    except (ConfigError, BadInput) as exc:
        return failed("error", safe_reason(str(exc)))
    return scrub(await _guard(item.run(target, body)))


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
            "quota": {
                name: _site_quota(entry, site)
                for name, entry in providers.APIS.items()
                if _connected(entry, site)
            },
        }
        for site in sites
    ]
    return scrub(ok(rows, thin=not rows, limit=len(rows)))
