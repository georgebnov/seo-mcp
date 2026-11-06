"""Microsoft Clarity live insights, cached for the UTC day."""

import asyncio
import fcntl
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from seo_mcp import NAME, net
from seo_mcp.config import Site
from seo_mcp.params import Params
from seo_mcp.results import Refused, Unsupported, ok

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
ENDPOINTS = (net.Endpoint("GET", "www.clarity.ms", r"/export-data/api/v1/project-live-insights"),)
CACHE_ENV = NAME.upper().replace("-", "_") + "_CACHE_DIR"
# The API allows 10 calls for each project each day, for the last 1 to 3 days.
WINDOW_DAYS = 3
THIN_SESSIONS = 100
ROWS_PER_METRIC = 5
DAILY_CALLS = 10
# The API cuts an answer at this many rows and has no paging.
API_ROWS = 1000
PAGE_MATCH = "The API has no URL filter. The server keeps the URL rows of this page, any scheme."
MISS_REASON = (
    f"Clarity has no row for this page. An answer has at most {API_ROWS} rows for each metric, "
    "so this is not proof of zero traffic."
)
BROKEN_LEDGER = "The Clarity call ledger is broken, so the server spends no call. Delete the file."

_lock = asyncio.Lock()


def _folder() -> Path:
    return Path(os.environ.get(CACHE_ENV) or f"~/.cache/{NAME}").expanduser()


def cache_path(host: str, days: int = WINDOW_DAYS) -> Path:
    return _folder() / f"clarity-{quote(host, safe='')}-{days}d.json"


def ledger_path(project: str) -> Path:
    return _folder() / f"clarity-calls-{quote(project, safe='')}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        same_day = datetime.fromisoformat(stored["cached_at"]).date() == datetime.now(UTC).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return stored if same_day else None


def _write_cache(path: Path, stored: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, name = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(stored, file)
    Path(name).replace(path)


def _used_today(path: Path, today: str) -> int:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return int(stored["used"]) if stored["day"] == today else 0
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        # A broken count must not read as 0: that would spend the quota without control.
        raise Refused(BROKEN_LEDGER) from None


def _spend(project: str) -> int:
    """Count 1 call before it goes out. A failed call counts: the API can still count it."""
    path = ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    today = datetime.now(UTC).date().isoformat()
    # Each agent session runs its own server process, so a lock inside 1 process is not enough.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        used = _used_today(path, today)
        if used >= DAILY_CALLS:
            raise Refused(
                f"This server has spent the {DAILY_CALLS} calls of this Clarity project for "
                "this UTC day. Use a cached answer, or call again after 00:00 UTC."
            )
        _write_cache(path, {"day": today, "used": used + 1})
    return used + 1


def project(site: Site) -> str:
    return site.fields.get("clarity_project", site.host)


def _headers(site: Site) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {site.secret('clarity_token')}",
        "Content-Type": "application/json",
        "User-Agent": NAME,
    }


async def _fetch(site: Site, days: int) -> tuple[dict[str, Any], bool]:
    path = cache_path(site.host, days)
    # 2 runs of 1 call, such as a page comparison, must not both miss the cache.
    async with _lock:
        stored = _read_cache(path)
        if stored is not None:
            return stored, True
        headers = _headers(site)
        _spend(project(site))
        params = {"numOfDays": days, "dimension1": "URL"}
        answer = await net.request(ENDPOINTS, "GET", API, params=params, headers=headers)
        stored = {"cached_at": datetime.now(UTC).isoformat(), "answer": answer}
        _write_cache(path, stored)
        return stored, False


def _number(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _page_key(url: str) -> tuple[str | None, str, str]:
    parts = urlsplit(url)
    return parts.hostname, parts.path or "/", parts.query


def _only_page(metrics: list[dict[str, Any]], page: str) -> list[dict[str, Any]]:
    wanted = _page_key(page)
    return [
        {
            **metric,
            "information": [
                item
                for item in metric.get("information") or []
                if _page_key(str(item.get("Url", ""))) == wanted
            ],
        }
        for metric in metrics
    ]


def _rows(metric: dict[str, Any]) -> list[dict[str, Any]]:
    items = metric.get("information") or []
    return [{"metric": metric.get("metricName"), **item} for item in items]


def _page_fields(metrics: list[dict[str, Any]], found: bool) -> dict[str, Any]:
    capped = any(len(metric.get("information") or []) >= API_ROWS for metric in metrics)
    fields: dict[str, Any] = {"page_match": PAGE_MATCH, "complete": not capped}
    if not found:
        fields["reason"] = MISS_REASON
    return fields


async def clarity(site: Site, params: Params) -> dict[str, Any]:
    days = params.last_days or WINDOW_DAYS
    if days > WINDOW_DAYS:
        raise Unsupported(["last_days"], "Clarity gives only the last 1, 2, or 3 days.")
    stored, from_cache = await _fetch(site, days)
    metrics = stored["answer"]
    shown = _only_page(metrics, params.page) if params.page else metrics
    traffic = [row for m in shown if m.get("metricName") == "Traffic" for row in _rows(m)]
    rows = [row for metric in shown for row in _rows(metric)[:ROWS_PER_METRIC]]
    found = bool(traffic) or not params.page
    sessions = int(sum(_number(row.get("totalSessionCount")) for row in traffic))
    extra = _page_fields(metrics, bool(rows)) if params.page else {}
    return ok(
        rows,
        thin=sessions < THIN_SESSIONS,
        limit=len(rows),
        window={"last_days": days},
        totals={"sessions": sessions if found else None},
        from_cache=from_cache,
        cached_at=stored["cached_at"],
        **extra,
    )


SOURCES = {"clarity": clarity}
ACCEPTS = {"clarity": frozenset({"last_days", "page"})}
