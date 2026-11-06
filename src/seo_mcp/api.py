"""The full path: 1 official read query. A provider lists its methods in APIS."""

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, NamedTuple

from seo_mcp.config import Site
from seo_mcp.params import BadInput

# A result goes into a model context, so a row limit over this is an error, not a silent cut.
MAX_ROWS = 1000
MAX_BYTES = 100_000
NO_FIELDS = frozenset[str]()

Run = Callable[[Site, dict[str, Any]], Awaitable[dict[str, Any]]]


class Method(NamedTuple):
    run: Run
    summary: str
    fields: frozenset[str] = NO_FIELDS
    required: frozenset[str] = NO_FIELDS


class Api(NamedTuple):
    title: str
    docs: str
    methods: dict[str, Method]
    options: dict[str, Any]
    gaps: tuple[str, ...]


def check_request(name: str, method: Method, request: dict[str, Any]) -> None:
    unknown = sorted(set(request) - method.fields)
    if unknown:
        known = ", ".join(sorted(method.fields)) or "no fields"
        raise BadInput(f"{name} has no field {', '.join(unknown)}. It takes {known}.")
    missing = sorted(method.required - set(request))
    if missing:
        raise BadInput(f"{name} needs {', '.join(missing)}.")


def one_of(name: str, value: Any, allowed: Iterable[str]) -> None:
    choices = tuple(allowed)
    if value not in choices:
        raise BadInput(f"{name} must be 1 of {', '.join(choices)}.")


def whole(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BadInput(f"{name} must be a whole number from {low} to {high}.")
    return value


def row_limit(name: str, value: Any, paging: str) -> int:
    """Give the limit to send. The limit of a GA4 request can be a number in text."""
    if value is None:
        return MAX_ROWS
    number = int(value) if isinstance(value, str) and value.isdecimal() else value
    if isinstance(number, int) and number > MAX_ROWS:
        raise BadInput(f"{name} can be at most {MAX_ROWS} here. Read more rows with {paging}.")
    return whole(name, number, 1, MAX_ROWS)


def as_rows(answer: Any) -> list[dict[str, Any]]:
    items = answer if isinstance(answer, list) else [answer]
    return [item if isinstance(item, dict) else {"value": item} for item in items]


def _fit(rows: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    kept = []
    for row in rows[:MAX_ROWS]:
        budget -= len(json.dumps(row, default=str)) + 2
        if budget < 0:
            break
        kept.append(row)
    return kept


def answer(
    rows: list[dict[str, Any]],
    *,
    more_rows: str,
    row_count: int | None = None,
    reason: str | None = None,
    thin: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a result. more_rows is yes, no, or possible, because an API may not know."""
    kept = _fit(rows, MAX_BYTES - len(json.dumps(extra, default=str)))
    truncated = len(kept) < len(rows)
    return {
        "status": "ok",
        "reason": reason,
        "thin_data": not rows if thin is None else thin,
        "rows_returned": len(kept),
        "row_count": row_count,
        "more_rows": "yes" if truncated else more_rows,
        "truncated": truncated,
        **extra,
        "rows": kept,
    }
