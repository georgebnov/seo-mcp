"""1 live call for identical requests, across the tasks and server processes of 1 cache folder.

Each agent runs its own server process, and 18 agents can ask the same thing in 1 audit. A file
lock lets 1 caller send. The others wait, then read the answer or the failure that it saved.
"""

import asyncio
import hashlib
import json
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from seo_mcp import net, quota
from seo_mcp.config import Site
from seo_mcp.secrets import safe_reason, scrub

ANSWER_SECONDS = 300
# A realtime report changes each minute, so a repeat gets only the burst of 1 audit step.
REALTIME_SECONDS = 30
# A failed call can count at the API or in a ledger, so the same request waits this long. This
# stops a burst of agents. It does not protect a day: a long outage still spends 1 call in each
# window. A read with no ledger retries a connection failure at once: that try costs nothing.
FAILURE_WINDOW_SECONDS = 60
PRUNE_SECONDS = 3600
PRUNE_BATCH = 100
KEY_FORMAT = 1

Until = Callable[[datetime], datetime]
Check = Callable[[Any], bool]

_trace: ContextVar[list[dict[str, Any]] | None] = ContextVar("shared_trace", default=None)


def folder() -> Path:
    return quota.folder() / "shared"


def after(seconds: int) -> Until:
    return lambda at: at + timedelta(seconds=seconds)


def end_of_utc_day(at: datetime) -> datetime:
    day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1)


def is_object(reply: Any) -> bool:
    return isinstance(reply, dict)


def holds(name: str) -> Check:
    """Each good answer of this read has the field."""
    return lambda reply: is_object(reply) and name in reply


def has_any(*names: str) -> Check:
    """Each good answer of this read has at least 1 of the fields, so {} is a failure."""
    return lambda reply: is_object(reply) and any(name in reply for name in names)


def documented(*names: str) -> Check:
    """The docs list all fields of the answer, and {} can be a real answer.

    {} and any 1 of the fields pass, so a real empty answer stays ok. Other text, such as
    {"message": "try later"}, is a failure: it must not read as "no data".
    """
    some = has_any(*names)
    return lambda reply: reply == {} or some(reply)


def entry(site: Site, *parts: Any) -> Path:
    """The file of a request. Only a digest reaches the disk, also of a key in a query."""
    text = json.dumps([KEY_FORMAT, site.host, *parts], sort_keys=True, default=str)
    return folder() / f"{hashlib.sha256(text.encode()).hexdigest()[:32]}.json"


def failure_path(path: Path) -> Path:
    return path.with_suffix(".failed.json")


def begin() -> None:
    """Start the list of this source. Each task of the source adds to the same list."""
    _trace.set([])


def shown() -> dict[str, Any]:
    """Tell a reader how old the answer is. The oldest part decides, and the first to end."""
    parts = _trace.get()
    if not parts:
        return {}
    oldest = min(part["cached_at"] for part in parts)
    return {
        "from_cache": all(part["from_cache"] for part in parts),
        "cached_at": oldest.isoformat(),
        "cache_age_seconds": int((datetime.now(UTC) - oldest).total_seconds()),
        "cache_expires_at": min(part["expires_at"] for part in parts).isoformat(),
    }


def _note(from_cache: bool, at: datetime, expires_at: datetime) -> None:
    parts = _trace.get()
    if parts is not None:
        parts.append({"from_cache": from_cache, "cached_at": at, "expires_at": expires_at})


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _saved(path: Path, good: Check, until: Until) -> dict[str, Any] | None:
    stored = _read_json(path)
    try:
        at = datetime.fromisoformat(stored["cached_at"])
        usable = at <= datetime.now(UTC) < until(at) and good(stored["answer"])
    except (ValueError, KeyError, TypeError):
        return None
    return {"answer": stored["answer"], "at": at, "expires_at": until(at)} if usable else None


def peek(path: Path, good: Check, until: Until) -> dict[str, Any] | None:
    """Read a saved answer with no lock, no send, and no spend.

    A miss proves nothing: the caller goes on as if it had no answer. Writes replace the file
    in 1 step, so a read never sees half of an answer.
    """
    return _saved(path, good, until)


def cite(saved: dict[str, Any]) -> None:
    """Show the age of a peeked answer that decided the result."""
    _note(True, saved["at"], saved["expires_at"])


def _recent_failure(path: Path) -> net.ApiError | None:
    stored = _read_json(failure_path(path))
    try:
        at = datetime.fromisoformat(stored["failed_at"])
        status, detail = stored["status"], stored.get("detail")
        age = (datetime.now(UTC) - at).total_seconds()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    fits = (status is None or type(status) is int) and (detail is None or type(detail) is str)
    if not 0 <= age < FAILURE_WINDOW_SECONDS or not fits:
        return None
    _note(True, at, at + timedelta(seconds=FAILURE_WINDOW_SECONDS))
    left = math.ceil(FAILURE_WINDOW_SECONDS - age)
    return net.ApiError(
        status,
        detail,
        note=f"The same request failed {int(age)} seconds ago, so the server did not send it "
        f"again and spent no call. It can send it after {left} seconds.",
    )


def _mark_failure(path: Path, status: int | None, detail: str | None) -> None:
    stored = {
        "failed_at": datetime.now(UTC).isoformat(),
        "status": status,
        "detail": safe_reason(detail) if detail else None,
    }
    quota.write_json(failure_path(path), stored)


def _prune(where: Path) -> None:
    """Remove old entries of this folder only. A ledger is outside it, and it must stay."""
    limit = datetime.now(UTC).timestamp() - PRUNE_SECONDS
    with os.scandir(where) as found:
        # Oldest first: a lock file or a fresh entry must not use up the batch.
        entries = [item for item in found if item.name.endswith(".json")]
    entries.sort(key=lambda item: item.stat().st_mtime)
    for item in entries[:PRUNE_BATCH]:
        path = Path(item.path)
        lock_file = where / f"{path.name.split('.')[0]}.lock"
        with lock_file.open("a") as lock, suppress(OSError):
            quota.try_lock(lock)
            if path.stat().st_mtime < limit:
                path.unlink()


@asynccontextmanager
async def _only_sender(path: Path) -> AsyncIterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        await quota.take_lock(lock)
        yield


async def _nothing() -> None:
    return None


async def once(
    path: Path,
    send: Callable[[Any], Awaitable[Any]],
    *,
    prepare: Callable[[], Awaitable[Any]] = _nothing,
    good: Check = is_object,
    until: Until = after(ANSWER_SECONDS),  # noqa: B008
) -> Any:
    """Give the saved answer, the failure of the window, or the answer of 1 live call.

    prepare runs before the request, and it is the only step that spends from a ledger: a
    repeat spends nothing, and a saved answer still serves at the limit. send is the only step
    that can have reached the API. Each failure of a spent call starts the failure window.
    """
    async with _only_sender(path):
        saved = _saved(path, good, until)
        if saved:
            _note(True, saved["at"], saved["expires_at"])
            return saved["answer"]
        failure = _recent_failure(path)
        if failure:
            raise failure
        ready = await prepare()
        try:
            reply = await send(ready)
            if not good(reply):
                raise net.ApiError(200, net.BAD_SHAPE)
        except net.ApiError as exc:
            if exc.sent or prepare is not _nothing:
                _mark_failure(path, exc.status, exc.detail)
                # A source can read a failure as an answer, such as a 404 of no data: it is
                # live, and the same request waits for the window.
                at = datetime.now(UTC)
                _note(False, at, at + timedelta(seconds=FAILURE_WINDOW_SECONDS))
            raise
        except asyncio.CancelledError:
            # The source timeout ended a call in flight, and the API can have counted it.
            _mark_failure(path, None, None)
            raise
        at = datetime.now(UTC)
        _note(False, at, until(at))
        # The answer is paid for: a full disk must not lose it.
        with suppress(OSError):
            quota.write_json(path, {"cached_at": at.isoformat(), "answer": scrub(reply)})
    if path.parent == folder():
        with suppress(OSError):
            _prune(path.parent)
    return reply


async def request(
    site: Site,
    allowed: tuple[net.Endpoint, ...],
    method: str,
    url: str,
    *,
    good: Check = is_object,
    keep: int = ANSWER_SECONDS,
    explain: bool = False,
    basis: str | None = None,
    **kwargs: Any,
) -> Any:
    """Send 1 read that has no ledger. good tells a good answer from a failed HTTP 200.

    The headers stay out of the key, because a token changes each hour. The URL and the body
    name the property, so a saved answer ends at the latest after keep seconds. basis goes only
    into the key: a fact that the answer is read with, so that a changed fact asks again.
    """
    net.check(allowed, method, url)
    sent = (kwargs.get("params"), kwargs.get("json"))
    path = entry(site, method, url, *sent, explain, keep, *([basis] if basis else []))

    async def send(_: None) -> Any:
        return await net.request(allowed, method, url, explain=explain, **kwargs)

    return await once(path, send, good=good, until=after(keep))
