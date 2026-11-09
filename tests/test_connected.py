"""A site is connected to a provider only when each field and each secret reference is there.

The server derives it from the sites file. A provider that is not connected is never called
for that site: it sends nothing, spends no quota, and serves no saved answer.
"""

import ast
import json
import os
import stat
from pathlib import Path

import pytest

from seo_mcp import config, connection, core, providers
from test_query_api import SITE

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp"
HOST = "www.example.com"
NO_BING_KEY = (
    "This site has no Bing connection: bing_api_key has no secret reference, so seo-mcp has no "
    "Bing data for it."
)
# A reference name that must never show in a result: a reason has only canonical names.
ODD_NAME = "ODD_REFERENCE_NAME_7f3a9c"


def edit(change) -> None:
    """Change the live sites file in place. The shared secrets are in body["secrets"]."""
    path = config.path()
    body = json.loads(path.read_text())
    change(body, next(iter(body["sites"].values())))
    path.write_text(json.dumps(body))


def drop_bing_key(body, entry) -> None:
    del body["secrets"]["bing_api_key"]


def cache_files() -> dict[str, bytes]:
    folder = Path(os.environ["SEO_MCP_CACHE_DIR"])
    return {str(p): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def security(tmp_path, monkeypatch):
    """A fake `security` tool that logs each call and answers with 1 exit code."""
    folder = tmp_path / "bin"
    folder.mkdir()
    log = tmp_path / "security.log"

    def install(code: int) -> Path:
        tool = folder / "security"
        tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit {code}\n')
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
        return log

    return install


def test_list_sites_shows_each_provider(live):
    (row,) = core.list_sites()["rows"]
    assert row["connected"] == dict.fromkeys(providers.APIS, True)
    assert row["missing"] == {}


def test_list_sites_names_what_is_missing(live, monkeypatch):
    monkeypatch.delenv("BING_KEY_ENV")
    edit(lambda body, entry: entry.pop("ga4_property_id"))
    (row,) = core.list_sites()["rows"]
    assert row["connected"] == {**dict.fromkeys(providers.APIS, True), "bing": False, "ga4": False}
    assert row["missing"] == {
        "bing": ["the environment variable that bing_api_key names is not set"],
        "ga4": ["the sites file has no ga4_property_id"],
    }
    assert "bing" not in row["quota"]
    assert live == []


async def test_a_missing_reference_gives_not_connected_and_sends_nothing(live):
    edit(drop_bing_key)
    data = await core.get_seo_data(HOST, ["bing_links", "search_queries"])
    bing = data["sources"]["bing_links"]
    assert bing["status"] == "not_connected"
    assert bing["reason"] == NO_BING_KEY
    assert bing["rows"] == []
    assert data["sources"]["search_queries"]["status"] == "ok"
    assert not [call for call in live if call.url.host == "ssl.bing.com"]


async def test_a_site_without_its_key_is_still_not_connected(live):
    """George: a site that exists with its key missing is still a missing connection."""
    edit(drop_bing_key)
    (row,) = core.list_sites()["rows"]
    assert row["connected"]["bing"] is False
    data = await core.query_api(HOST, "bing", "GetQueryStats", {})
    assert data["status"] == "not_connected"
    assert data["reason"] == NO_BING_KEY
    assert live == []


async def test_not_connected_beats_an_unsupported_parameter(live):
    edit(drop_bing_key)
    data = await core.get_seo_data(HOST, ["bing_links"], page="/a")
    assert data["sources"]["bing_links"]["status"] == "not_connected"
    assert data["sources"]["bing_links"]["reason"] == NO_BING_KEY
    assert live == []


async def test_a_saved_answer_is_not_served_when_the_key_is_gone(live, tmp_path):
    first = await core.query_api(HOST, "search_console", "sitemaps", {})
    assert first["status"] == "ok"
    count = len(live)
    (tmp_path / "account.json").rename(tmp_path / "moved.json")
    saved = cache_files()
    data = await core.query_api(HOST, "search_console", "sitemaps", {})
    assert data["status"] == "not_connected"
    assert data["reason"].startswith("This site has no Search Console connection: the file")
    assert data["rows"] == []
    assert "from_cache" not in data
    sources = await core.get_seo_data(HOST, ["sitemaps"])
    assert sources["sources"]["sitemaps"]["status"] == "not_connected"
    assert len(live) == count
    assert cache_files() == saved


async def test_no_quota_is_spent_for_a_missing_clarity_token(live, monkeypatch):
    monkeypatch.delenv("CLARITY_ENV")
    saved = cache_files()
    data = await core.get_seo_data(HOST, ["clarity"])
    assert data["sources"]["clarity"]["status"] == "not_connected"
    assert "calls_left" not in data["sources"]["clarity"]
    assert live == []
    assert cache_files() == saved


async def test_a_task_step_of_a_provider_that_is_not_connected(live, monkeypatch):
    monkeypatch.delenv("CLARITY_ENV")
    found = await core.query_api(HOST, task="dead clicks")
    clarity = [row for row in found["rows"] if row["api"] == "clarity"]
    assert clarity
    assert {row["status"] for row in clarity} == {"not_connected"}
    assert clarity[0]["reason"].startswith("This site has no Clarity connection")
    assert "now" not in clarity[0]["quota"]
    step = clarity[0]["steps"][0]["args"]
    data = await core.query_api(step["site"], step["api"], step["method"], step["request"])
    assert data["status"] == "not_connected"
    assert live == []


async def test_a_missing_keychain_item_is_not_connected(live, security):
    log = security(44)
    edit(lambda body, entry: body["secrets"].update(bing_api_key={"keychain": "bing-item"}))
    data = await core.get_seo_data(HOST, ["bing_links"])
    reason = data["sources"]["bing_links"]["reason"]
    assert reason == (
        "This site has no Bing connection: the Keychain item that bing_api_key names does not "
        "exist, so seo-mcp has no Bing data for it."
    )
    assert "-w" not in log.read_text().split()
    assert live == []


async def test_a_keychain_that_cannot_be_checked_is_not_connected(live, monkeypatch):
    edit(lambda body, entry: body["secrets"].update(bing_api_key={"keychain": "bing-item"}))
    monkeypatch.setenv("PATH", "/nonexistent")
    data = await core.get_seo_data(HOST, ["bing_links"])
    assert data["sources"]["bing_links"]["status"] == "not_connected"
    assert "cannot check the Keychain item" in data["sources"]["bing_links"]["reason"]


async def test_1_call_checks_each_reference_once(live, security):
    """page_speed and chrome_field_data share 1 key: 1 probe, then each source reads it."""
    log = security(0)
    edit(lambda body, entry: body["secrets"].update(google_api_key={"keychain": "google-item"}))
    await core.get_seo_data(HOST, ["page_speed", "chrome_field_data"])
    probes = [line for line in log.read_text().splitlines() if "-w" not in line.split()]
    assert probes == ["find-generic-password -s google-item"]


async def test_a_reason_never_shows_a_reference_name(live):
    edit(lambda body, entry: body["secrets"].update(bing_api_key={"env": ODD_NAME}))
    data = await core.get_seo_data(HOST, ["bing_links"])
    assert data["sources"]["bing_links"]["status"] == "not_connected"
    listed = core.list_sites()
    assert ODD_NAME not in json.dumps(data) + json.dumps(listed)


def _nothing_connected(body, entry) -> None:
    """Every field is there, and each secret reference names an env that is not set."""
    names = set().union(*(needs.secrets | needs.optional for needs in connection.NEEDS.values()))
    body["secrets"] = {name: {"env": f"UNSET_{name.upper()}"} for name in names}
    entry.pop("secrets", None)


@pytest.mark.parametrize("name", sorted(providers.SOURCES))
async def test_each_source_checks_the_connection(live, name):
    """The guard: a source that skips the gate gives its own reason, or sends a request."""
    edit(_nothing_connected)
    data = await core.get_seo_data(HOST, [name])
    result = data["sources"][name]
    assert result["status"] == "not_connected", name
    assert result["reason"].startswith("This site has no "), (
        f"{name} did not pass the connection gate in src/seo_mcp/providers/__init__.py: "
        f"{result['reason']}"
    )
    assert live == [], f"{name} sent a request for a provider that is not connected."


@pytest.mark.parametrize(
    ("api", "method"),
    sorted((api, method) for api, entry in providers.APIS.items() for method in entry.methods),
)
async def test_each_method_checks_the_connection(live, api, method):
    """The guard: a method that skips the gate gives its own reason, or sends a request."""
    edit(_nothing_connected)
    item = providers.APIS[api].methods[method]
    data = await core.query_api(HOST, api, method, dict.fromkeys(item.required, "x"))
    assert data["status"] == "not_connected", f"{api}.{method}: {data['reason']}"
    assert data["reason"].startswith("This site has no "), (
        f"{api}.{method} did not pass the connection gate in src/seo_mcp/providers/__init__.py."
    )
    assert live == [], f"{api}.{method} sent a request for a provider that is not connected."


def test_each_source_has_1_provider():
    assert set(providers.PROVIDER_OF) == set(providers.SOURCES)
    assert set(providers.PROVIDER_OF.values()) <= set(providers.APIS)
    assert set(connection.NEEDS) == set(providers.APIS)


def test_a_name_in_2_modules_is_an_error():
    class One:
        SOURCES = {"same": None}

    class Two:
        SOURCES = {"same": None}

    with pytest.raises(ValueError, match="same"):
        providers.merge((One, Two), "SOURCES")


def _provider_imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "seo_mcp.providers":
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "seo_mcp.providers."
        ):
            found.append(node.module or "")
        elif isinstance(node, ast.Attribute) and ast.unparse(node) == "providers.MODULES":
            found.append("providers.MODULES")
    return found


def test_only_the_gated_registry_reaches_a_provider():
    """The guard: code outside providers/ calls a provider only through the gated registry."""
    wrong = {
        path.name: names
        for path in sorted(SOURCE.glob("*.py"))
        if (names := sorted(set(_provider_imports(path)) - {"Source"}))
    }
    assert not wrong, (
        f"{wrong} reach a provider module past the connection gate. Call providers.SOURCES or "
        "providers.APIS: src/seo_mcp/providers/__init__.py wraps each one with the gate."
    )


def test_a_site_with_all_names_is_connected(write_config, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "x")
    write_config(SITE, {"bing_api_key": {"env": "CLARITY_ENV"}})
    (site,) = config.load()
    assert connection.connected(site, "bing") is True
    assert connection.connected(site, "search_console") is False
