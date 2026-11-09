"""A saved answer, a failure, and a shared call belong to 1 credential. The ledger does not."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, quota, shared
from seo_mcp.providers import clarity, gsc, pagespeed
from test_query_api import HOST, RANGE, SITE
from test_quota import DASHBOARD, INSPECT, replies, run, sent

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
CLARITY_A = "clarity-token-value-1234"
CLARITY_B = "clarity-token-value-9999"
GA4_REPORT = ("run_report", {"metrics": [{"name": "sessions"}]})


def other_account(tmp_path: Path, account: dict) -> dict:
    """The same key file shape with another client_email: another principal."""
    first = json.loads(Path(account["google_service_account"]["file"]).read_text())
    path = tmp_path / "other-account.json"
    path.write_text(json.dumps({**first, "client_email": "b@example.com"}))
    return {"google_service_account": {"file": str(path)}}


def env_change(monkeypatch, name: str, value: str):
    return lambda: monkeypatch.setenv(name, value)


def account_change(tmp_path, write_config, account, live_secrets):
    def change():
        write_config(SITE, {**live_secrets, **other_account(tmp_path, account)})

    return change


def shared_secrets(account):
    return {
        **account,
        "google_api_key": {"env": "GOOGLE_KEY_ENV"},
        "bing_api_key": {"env": "BING_KEY_ENV"},
    }


CALLS = [
    ("clarity", *DASHBOARD),
    ("pagespeed", "run", {"url": "/a"}),
    ("crux", "record", {}),
    ("search_console", "search_analytics", RANGE),
    ("search_console", *INSPECT),
    ("ga4", *GA4_REPORT),
    ("bing", "GetQueryStats", {}),
]


def switch(api, monkeypatch, tmp_path, write_config, account):
    if api == "clarity":
        return env_change(monkeypatch, "CLARITY_ENV", CLARITY_B)
    if api in ("pagespeed", "crux"):
        return env_change(monkeypatch, "GOOGLE_KEY_ENV", "google-key-value-9999")
    if api == "bing":
        return env_change(monkeypatch, "BING_KEY_ENV", "bing-key-value-9999")
    return account_change(tmp_path, write_config, account, shared_secrets(account))


@pytest.mark.parametrize(("api", "method", "body"), CALLS)
async def test_2_credentials_of_1_request_send_2_times(
    live, monkeypatch, tmp_path, write_config, account, api, method, body
):
    first = await core.query_api(HOST, api, method, body)
    assert first["status"] == "ok"
    count = len(sent(live))
    switch(api, monkeypatch, tmp_path, write_config, account)()
    second = await core.query_api(HOST, api, method, body)
    assert second["status"] == "ok"
    assert second["from_cache"] is False
    assert len(sent(live)) > count


@pytest.mark.parametrize(("api", "method", "body"), CALLS)
async def test_1_credential_of_1_request_sends_1_time(live, api, method, body):
    await core.query_api(HOST, api, method, body)
    count = len(sent(live))
    second = await core.query_api(HOST, api, method, body)
    assert second["from_cache"] is True
    assert len(sent(live)) == count


async def test_a_new_clarity_token_ends_the_failure_window(live, fake_net, monkeypatch):
    def reply(request):
        failing = request.headers.get("authorization") == f"Bearer {CLARITY_A}"
        return httpx2.Response(403) if "clarity" in request.url.host and failing else None

    calls = fake_net(replies(reply))
    first = await run("clarity", DASHBOARD)
    assert first["status"] == "error"
    again = await run("clarity", DASHBOARD)
    assert "did not send it again" in again["reason"]
    monkeypatch.setenv("CLARITY_ENV", CLARITY_B)
    fixed = await run("clarity", DASHBOARD)
    assert fixed["status"] == "ok"
    assert len(sent(calls)) == 2


async def test_a_new_google_api_key_ends_the_failure_window(live, fake_net, monkeypatch):
    def reply(request):
        bad = request.headers.get("x-goog-api-key") == "google-key-value-1234"
        return httpx2.Response(400) if "runPagespeed" in request.url.path and bad else None

    calls = fake_net(replies(reply))
    assert (await run("pagespeed", ("run", {"url": "/a"})))["status"] == "error"
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-9999")
    assert (await run("pagespeed", ("run", {"url": "/a"})))["status"] == "ok"
    assert len(sent(calls)) == 2


def _old_entry(*parts) -> Path:
    """The file name of format 1: the host and the request, with no credential."""
    text = json.dumps([1, HOST, *parts], sort_keys=True, default=str)
    return shared.folder() / f"{hashlib.sha256(text.encode()).hexdigest()[:32]}.json"


def _plant(path: Path, answer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {"cached_at": datetime.now(UTC).isoformat(), "answer": answer}
    path.write_text(json.dumps(stored))


async def test_an_entry_of_the_old_key_format_is_not_read(live):
    params = [("url", f"https://{HOST}/a")]
    old = _old_entry("GET", pagespeed.PSI_API, params, None, True, shared.ANSWER_SECONDS)
    _plant(old, {"lighthouseResult": {"planted": True}})
    data = await run("pagespeed", ("run", {"url": "/a"}))
    assert data["from_cache"] is False
    assert "planted" not in json.dumps(data)


async def test_an_old_clarity_file_is_not_read(live):
    body = {"query": DASHBOARD[1]["query"], "timezone": "UTC"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    _plant(quota.folder() / f"clarity-{HOST}-dashboard-{digest}.json", {"data": [{"old": 1}]})
    data = await run("clarity", DASHBOARD)
    assert data["from_cache"] is False
    assert data["calls_used_today"] == 1


async def test_a_new_clarity_token_keeps_the_ledger_count(live, monkeypatch):
    await run("clarity", DASHBOARD)
    monkeypatch.setenv("CLARITY_ENV", CLARITY_B)
    data = await run("clarity", DASHBOARD)
    assert data["from_cache"] is False
    assert data["calls_used_today"] == 2
    assert quota.view(clarity.LEDGER, HOST)["calls_used_today"] == 2


async def test_a_new_account_keeps_the_inspection_count(live, tmp_path, write_config, account):
    await run("search_console", INSPECT)
    write_config(SITE, {**shared_secrets(account), **other_account(tmp_path, account)})
    data = await run("search_console", INSPECT)
    assert data["from_cache"] is False
    assert quota.view(gsc.LEDGER, SITE["gsc_property"])["calls_used_today"] == 2


def test_an_identity_holds_no_secret_byte():
    made = shared.identity("clarity_token", CLARITY_A)
    assert CLARITY_A not in repr(made)
    assert made == shared.identity("clarity_token", CLARITY_A)
    assert made != shared.identity("clarity_token", CLARITY_B)
    assert made != shared.identity("google_api_key", CLARITY_A)
    assert shared.identity("google_api_key", None) != shared.identity("google_api_key", "none")


def test_the_salt_file_is_private():
    shared.identity("clarity_token", CLARITY_A)
    mode = shared.salt_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_parallel_processes_make_1_salt(tmp_path):
    code = (
        "from seo_mcp import shared; "
        "print(shared.identity('clarity_token', 'clarity-token-value-1234').digest)"
    )
    env = {"SEO_MCP_CACHE_DIR": str(tmp_path / "race"), "PATH": "/usr/bin:/bin"}
    started = [
        subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
        for _ in range(12)
    ]
    digests = {process.communicate()[0].strip() for process in started}
    assert len(digests) == 1
    assert all(started_process.returncode == 0 for started_process in started)


# The guard: a key with no credential cannot reach the cache.
async def test_once_refuses_a_path_with_no_identity(tmp_path):
    async def send(_):
        return {}

    with pytest.raises(TypeError, match="shared.entry"):
        await shared.once(tmp_path / "plain.json", send)


def test_a_key_needs_a_real_identity():
    site = core.config.Site(HOST, Path("/repo"))
    with pytest.raises(TypeError, match="shared.identity"):
        shared.entry(site, "x", auth="clarity_token")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        shared.entry(site, "x")  # type: ignore[call-arg]


def test_only_shared_makes_a_keyed_path():
    makers = [
        str(path.relative_to(SOURCE))
        for path in sorted(SOURCE.rglob("*.py"))
        if "Keyed(" in path.read_text() and path.name != "shared.py"
    ]
    assert makers == [], (
        f"{makers} build a cache path by hand. Use shared.entry or shared.named with "
        "auth=shared.identity(...), so that a new key does not read the old answer."
    )
