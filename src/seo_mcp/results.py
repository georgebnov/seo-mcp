"""The result of 1 source: status, reason, thin_data, and rows."""

from typing import Any

ROW_LIMIT = 25


class NotConnected(Exception):
    """The site has no field or secret for this source. The text is safe to show."""


def ok(
    rows: list[dict[str, Any]],
    *,
    thin: bool,
    reason: str | None = None,
    limit: int = ROW_LIMIT,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "reason": reason,
        "thin_data": thin,
        **extra,
        "rows_total": len(rows),
        "truncated": len(rows) > limit,
        "rows": rows[:limit],
    }


def failed(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "thin_data": True, "rows": []}
