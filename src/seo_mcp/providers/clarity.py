"""Microsoft Clarity live insights, cached for the UTC day."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from seo_mcp import NAME, net
from seo_mcp.config import Site
from seo_mcp.results import ok

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
ENDPOINTS = (net.Endpoint("GET", "www.clarity.ms", r"/export-data/api/v1/project-live-insights"),)
CACHE_ENV = NAME.upper().replace("-", "_") + "_CACHE_DIR"
# The API allows 10 calls for each project each day, for the last 1 to 3 days.
WINDOW_DAYS = 3
THIN_SESSIONS = 100
ROWS_PER_METRIC = 5


def cache_path(host: str) -> Path:
    folder = Path(os.environ.get(CACHE_ENV) or f"~/.cache/{NAME}").expanduser()
    return folder / f"clarity-{quote(host, safe='')}.json"


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


def _number(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _rows(metric: dict[str, Any]) -> list[dict[str, Any]]:
    items = metric.get("information") or []
    return [{"metric": metric.get("metricName"), **item} for item in items]


async def clarity(site: Site) -> dict[str, Any]:
    path = cache_path(site.host)
    stored = _read_cache(path)
    from_cache = stored is not None
    if stored is None:
        params = {"numOfDays": WINDOW_DAYS, "dimension1": "URL"}
        headers = {
            "Authorization": f"Bearer {site.secret('clarity_token')}",
            "Content-Type": "application/json",
            "User-Agent": NAME,
        }
        answer = await net.request(ENDPOINTS, "GET", API, params=params, headers=headers)
        stored = {"cached_at": datetime.now(UTC).isoformat(), "answer": answer}
        _write_cache(path, stored)
    metrics = stored["answer"]
    traffic = [row for m in metrics if m.get("metricName") == "Traffic" for row in _rows(m)]
    sessions = int(sum(_number(row.get("totalSessionCount")) for row in traffic))
    rows = [row for metric in metrics for row in _rows(metric)[:ROWS_PER_METRIC]]
    return ok(
        rows,
        thin=sessions < THIN_SESSIONS,
        limit=len(rows),
        window={"last_days": WINDOW_DAYS},
        totals={"sessions": sessions},
        from_cache=from_cache,
        cached_at=stored["cached_at"],
    )


SOURCES = {"clarity": clarity}
