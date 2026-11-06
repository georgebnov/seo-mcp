"""Local daily call counts. This module opens no connection.

A ledger counts the calls of 1 key in the cache folder, before each call goes out. Each agent
session runs its own server process, so a count is safe only under a file lock.
"""

import asyncio
import fcntl
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any, NamedTuple
from urllib.parse import quote

from seo_mcp import NAME, net
from seo_mcp.results import Refused

CACHE_ENV = NAME.upper().replace("-", "_") + "_CACHE_DIR"
COUNTED = "Only the calls of this machine, through this cache folder."
RESET_ASSUMED = "The docs state no reset time, so this time is an assumption."

LOCK_POLL_SECONDS = 0.05
# A holder keeps a ledger lock only to read and write 1 small file, so this wait is long.
LOCK_WAIT_SECONDS: float = 2

Reader = Callable[[], dict[str, Any]]

_reader: ContextVar[Reader | None] = ContextVar("quota_reader", default=None)


class Ledger(NamedTuple):
    name: str
    title: str
    limit: int


def folder() -> Path:
    return Path(os.environ.get(CACHE_ENV) or f"~/.cache/{NAME}").expanduser()


def path(ledger: Ledger, key: str) -> Path:
    return folder() / f"{ledger.name}-{quote(key, safe='')}.json"


def write_json(target: Path, stored: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, name = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(stored, file)
    Path(name).replace(target)


async def take_lock(lock: IO[str], wait: float | None = None) -> bool:
    """Poll for a lock, so that a stuck process cannot freeze the event loop of this server.

    Each open file has its own flock, so this also separates 2 tasks of 1 process. The kernel
    frees the lock when a process dies. With no wait, the source timeout ends the poll.
    """
    deadline = None if wait is None else time.monotonic() + wait
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(LOCK_POLL_SECONDS)


@asynccontextmanager
async def locked(target: Path) -> AsyncIterator[None]:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.with_suffix(".lock").open("a") as lock:
        if not await take_lock(lock, LOCK_WAIT_SECONDS):
            raise Refused(
                f"The file {target.name} is busy: another process held its lock for "
                f"{LOCK_WAIT_SECONDS:g} seconds, so the server sends no call. Call again soon."
            )
        yield


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _next_reset() -> str:
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=1)).isoformat()


def _broken(ledger: Ledger, file: Path) -> str:
    return (
        f"The call ledger {file.name} of this {ledger.title} is broken, so the server spends no "
        'call. Write the real count in it, in the form {"day": "2026-01-31", "used": 3}.'
    )


def _is_past_or_today(day: Any) -> bool:
    """A count of an older day restarts at 0, so a day must prove that it is not a wrong one."""
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return parsed.isoformat() == day and parsed <= datetime.now(UTC).date()


def _read(ledger: Ledger, file: Path, today: str) -> tuple[int, bool]:
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
        used = stored["used"]
        day = stored["day"]
        blocked = stored.get("blocked", False)
    except FileNotFoundError:
        return 0, False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # A broken count must not read as 0: that would spend the quota without control.
        raise Refused(_broken(ledger, file)) from None
    wrong = type(used) is not int or used < 0 or type(blocked) is not bool
    if wrong or not _is_past_or_today(day):
        raise Refused(_broken(ledger, file))
    return (used, blocked) if day == today else (0, False)


def _warning(ledger: Ledger, left: int) -> str | None:
    if left > max(2, ledger.limit // 10):
        return None
    return f"Only {left} of {ledger.limit} calls of this {ledger.title} are left for this UTC day."


def view(ledger: Ledger, key: str) -> dict[str, Any]:
    """Read a count with no lock and no error. A broken count shows as unknown, never as 0."""
    fields: dict[str, Any] = {
        "calls_used_today": None,
        "daily_limit": ledger.limit,
        "calls_left": None,
        "resets_at": _next_reset(),
        "reset_stated": False,
        "counted": COUNTED,
    }
    try:
        used, blocked = _read(ledger, path(ledger, key), _today())
    except Refused as exc:
        return {**fields, "quota_warning": str(exc)}
    left = 0 if blocked else max(ledger.limit - used, 0)
    fields |= {"calls_used_today": used, "calls_left": left}
    if blocked:
        fields["blocked_by_api"] = True
    warning = _warning(ledger, left)
    return {**fields, "quota_warning": warning} if warning else fields


def _refusal(ledger: Ledger, used: int, blocked: bool, count: int) -> str:
    reset = f"Use a cached answer, or call again after {_next_reset()}. {RESET_ASSUMED}"
    if blocked:
        return f"The API answered HTTP 429 for this {ledger.title} on this UTC day. {reset}"
    if count == 1:
        spent = f"has spent the {ledger.limit} calls of this {ledger.title} for this UTC day"
        return f"This server {spent}. {reset}"
    left = max(ledger.limit - used, 0)
    return (
        f"This request needs {count} calls, and this {ledger.title} has {left} of "
        f"{ledger.limit} left for this UTC day. {reset}"
    )


async def spend(ledger: Ledger, key: str, count: int = 1) -> net.Receipt:
    """Count calls before they go out, all or none. A failed call counts: the API can too."""
    file = path(ledger, key)
    async with locked(file):
        today = _today()
        used, blocked = _read(ledger, file, today)
        if blocked or used + count > ledger.limit:
            raise Refused(_refusal(ledger, used, blocked, count))
        write_json(file, {"day": today, "used": used + count})
    return net.Receipt(ledger.name, count, today)


async def block(ledger: Ledger, key: str, receipt: net.Receipt) -> None:
    """Keep the real count, and stop the day. A late answer must not stop the next day."""
    file = path(ledger, key)
    async with locked(file):
        used, _ = _read(ledger, file, receipt.day)
        if _today() == receipt.day:
            write_json(file, {"day": receipt.day, "used": used, "blocked": True})


def show(reader: Reader) -> None:
    """Name the quota of this source. core._guard adds it to the result, also to a failure."""
    _reader.set(reader)


def forget() -> None:
    _reader.set(None)


def shown() -> dict[str, Any]:
    reader = _reader.get()
    return reader() if reader else {}
