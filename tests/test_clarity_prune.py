"""A Clarity answer of an old UTC day goes away, and each other file of the cache folder stays.

A Clarity answer is in the cache folder itself, and it serves only on the UTC day of its call.
Each new token adds new files, so with no prune the folder grows each day.
"""

import ast
import fcntl
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

from seo_mcp import quota, shared
from seo_mcp.providers import clarity
from test_query_api import HOST
from test_quota import DASHBOARD, run, set_ledger

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
DIGEST = "0123456789abcdef0123456789abcdef"


def start_of_yesterday() -> float:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=1)).timestamp()


def made(name: str, at: float, text: str = "{}") -> Path:
    path = quota.folder() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (at, at))
    return path


def answer_name(project: str = "proj", rest: str = "dashboard_query-abc") -> str:
    return f"clarity-{project}-{rest}-k{shared.KEY_FORMAT}-{DIGEST}"


async def test_an_old_clarity_answer_goes_and_today_and_yesterday_stay(live):
    old = start_of_yesterday() - 1
    stale = made(f"{answer_name()}.json", old)
    stale_failure = made(f"{answer_name()}.failed.json", old)
    yesterday = made(f"{answer_name(rest='28d-URL')}.json", start_of_yesterday() + 1)
    today = made(f"{answer_name(rest='3d-URL')}.json", datetime.now(UTC).timestamp())
    await run("clarity", DASHBOARD)
    assert not stale.exists()
    assert not stale_failure.exists()
    assert yesterday.exists()
    assert today.exists()


async def test_the_ledger_the_salt_locks_and_foreign_files_stay(live):
    old = start_of_yesterday() - 86_400
    set_ledger(clarity.LEDGER, HOST, used=3)
    ledger = quota.path(clarity.LEDGER, HOST)
    os.utime(ledger, (old, old))
    shared.identity("clarity_token", "x")
    os.utime(shared.salt_path(), (old, old))
    keep = [
        ledger,
        shared.salt_path(),
        made(f"{answer_name()}.lock", old, ""),
        made("clarity-calls-proj.json", old),
        made("clarity-mine.json", old),
        made(f"{answer_name()}.json.bak", old),
        made(f"other-{DIGEST}-k2-{DIGEST}.json", old),
        made("tmpab12cd34", old),
        made("notes.txt", old),
    ]
    await run("clarity", DASHBOARD)
    assert [path for path in keep if not path.exists()] == []
    assert quota.view(clarity.LEDGER, HOST)["calls_used_today"] == 4


async def test_a_saved_answer_also_prunes(live):
    await run("clarity", DASHBOARD)
    stale = made(f"{answer_name()}.json", start_of_yesterday() - 1)
    data = await run("clarity", DASHBOARD)
    assert data["from_cache"] is True
    assert not stale.exists()


async def test_an_answer_in_use_stays(live):
    """The lock of a project with a dot is the lock of the answer, also for its failure twin."""
    old = start_of_yesterday() - 1
    stem = answer_name(project="www.example.com")
    files = [made(f"{stem}.json", old), made(f"{stem}.failed.json", old)]
    with (quota.folder() / f"{stem}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await run("clarity", DASHBOARD)
        assert all(path.exists() for path in files)
    await run("clarity", DASHBOARD)
    assert not any(path.exists() for path in files)


def test_parallel_pruners_remove_each_old_answer_and_fail_none(tmp_path):
    folder = tmp_path / "parallel"
    folder.mkdir()
    old = start_of_yesterday() - 1
    for number in range(300):
        path = folder / f"clarity-p{number}-x-k2-{DIGEST}.json"
        path.write_text("{}")
        os.utime(path, (old, old))
    script = textwrap.dedent(
        """
        import os
        from seo_mcp import shared, quota
        for _ in range(5):
            shared.prune_day_answers(quota.folder())
        """
    )
    env = {**os.environ, "SEO_MCP_CACHE_DIR": str(folder)}
    workers = [
        subprocess.Popen([sys.executable, "-c", script], env=env, stderr=subprocess.PIPE)
        for _ in range(6)
    ]
    errors = [worker.communicate()[1].decode() for worker in workers]
    assert [worker.returncode for worker in workers] == [0] * 6, errors
    assert sorted(path.name for path in folder.iterdir() if path.suffix == ".json") == []


def test_each_named_answer_is_a_clarity_day_answer():
    """Only Clarity answers are in the cache folder itself, and the prune knows their names.

    An answer of another lifetime there would stay forever, or go before its end.
    """
    callers = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "named":
                callers.append(path.relative_to(SOURCE).as_posix())
    assert set(callers) <= {"providers/clarity.py"}, (
        f"shared.named is called in {sorted(set(callers))}. Only providers/clarity.py may call "
        "it: shared.prune_day_answers removes only Clarity answers of an old UTC day. Use "
        "shared.entry for an answer in shared/."
    )
    auth = shared.identity("clarity_token", "x")
    paths = [
        clarity.cache_path("www.example.com", auth),
        clarity._body_path("www.example.com", "recordings", {"a": 1}, auth),
    ]
    for path in paths:
        assert shared.DAY_ANSWER.fullmatch(path.name), path.name
        assert shared.DAY_ANSWER.fullmatch(shared.failure_path(path).name), path.name
