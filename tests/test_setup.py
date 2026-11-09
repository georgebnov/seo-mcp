"""`seo-mcp setup status`: a local check of the sites file that reads no secret value."""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from seo_mcp import config, core, providers, server, setup

SOURCE = Path(__file__).parent.parent / "src" / "seo_mcp" / "providers"
VALUE = "secret-value-that-must-not-print-1234"
FULL = {
    "gsc_property": "sc-domain:example.com",
    "ga4_property_id": "123456789",
    "bing_site_url": "https://www.example.com/",
}


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    """A fake `security` tool: it logs its arguments, and prints a value as the real one can."""
    folder = tmp_path / "bin"
    folder.mkdir()
    log = tmp_path / "security.log"

    def install(code: int) -> Path:
        tool = folder / "security"
        tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho {VALUE}\nexit {code}\n')
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
        return log

    return install


@pytest.fixture
def ready(write_config, tmp_path, keychain, monkeypatch):
    account = tmp_path / "account.json"
    account.write_text(VALUE)
    monkeypatch.setenv("SETUP_GOOGLE_KEY", VALUE)
    shared = {
        "google_service_account": {"file": str(account)},
        "google_api_key": {"env": "SETUP_GOOGLE_KEY"},
        "bing_api_key": {"keychain": "bing-item"},
    }
    log = keychain(0)
    write_config({**FULL, "secrets": {"clarity_token": {"keychain": "clarity-item"}}}, shared)
    return log


def run(capsys, *args: str) -> tuple[int, str]:
    with pytest.raises(SystemExit) as done:
        server.main(["setup", "status", *args])
    out = capsys.readouterr()
    return int(done.value.code or 0), out.out + out.err


def states(report: dict) -> dict[str, str]:
    return {row["provider"]: row["state"] for row in report["sites"][0]["providers"]}


def test_a_ready_site_exits_0_and_reads_no_value(ready, capsys, fake_net):
    calls = fake_net(lambda request: pytest.fail("status made a network call"))
    code, out = run(capsys)
    assert code == 0
    assert VALUE not in out
    assert "www.example.com" in out
    assert all(re.search(rf"{name}\s+ready", out) for name in providers.APIS)
    assert not calls
    asked = ready.read_text()
    assert "find-generic-password" in asked
    assert "-w" not in asked.split() and "-g" not in asked.split()


def test_json_names_each_provider_once(ready, capsys):
    code, out = run(capsys, "--json")
    report = json.loads(out)
    assert code == 0
    assert report["ready"] is True
    assert states(report) == dict.fromkeys(providers.APIS, "ready")


def test_each_gap_has_a_state_and_a_next_step(write_config, tmp_path, keychain, capsys):
    keychain(44)
    shared = {
        "google_service_account": {"file": str(tmp_path / "no-such-file.json")},
        "bing_api_key": {"keychain": "missing-item"},
    }
    write_config({"gsc_property": "sc-domain:example.com"}, shared)
    code, out = run(capsys, "--json")
    report = json.loads(out)
    assert code == 1
    assert report["ready"] is False
    assert states(report) == {
        "search_console": "secret not found",
        "ga4": "missing field",
        "pagespeed": "ready",
        "crux": "missing secret reference",
        "bing": "missing field",
        "clarity": "missing secret reference",
    }
    rows = {row["provider"]: row for row in report["sites"][0]["providers"]}
    steps = {gap["name"]: gap["step"] for row in rows.values() for gap in row["gaps"]}
    assert "ga4_property_id" in steps["ga4_property_id"]
    assert "no-such-file.json" in steps["google_service_account"]
    assert "missing-item" in steps["bing_api_key"]
    assert "clarity_token" in steps["clarity_token"]
    assert {gap["state"] for gap in rows["bing"]["gaps"]} == {"missing field", "secret not found"}


def test_an_unset_env_name_is_not_found(write_config, capsys, monkeypatch):
    monkeypatch.delenv("SETUP_NOT_SET", raising=False)
    write_config({}, {"google_api_key": {"env": "SETUP_NOT_SET"}})
    code, out = run(capsys, "--json")
    gap = next(row for row in json.loads(out)["sites"][0]["providers"] if row["provider"] == "crux")
    assert code == 1
    assert gap["state"] == "secret not found"
    assert "SETUP_NOT_SET" in gap["gaps"][0]["step"]


def test_an_unreadable_file_still_counts_as_found(write_config, tmp_path, capsys):
    """The check looks only for the file, so a file that it cannot read proves no read."""
    secret = tmp_path / "locked.json"
    secret.write_text(VALUE)
    secret.chmod(0)
    write_config(
        {"gsc_property": "sc-domain:example.com"}, {"google_service_account": {"file": str(secret)}}
    )
    code, out = run(capsys, "--json")
    assert states(json.loads(out))["search_console"] == "ready"
    assert VALUE not in out


def test_no_keychain_tool_is_not_checked(write_config, tmp_path, capsys, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    write_config({}, {"google_api_key": {"keychain": "google-item"}})
    code, out = run(capsys, "--json")
    assert code == 1
    assert states(json.loads(out))["crux"] == "not checked"


def test_text_output_names_the_step(write_config, capsys):
    write_config()
    code, out = run(capsys)
    assert code == 1
    assert "missing field" in out
    assert 'Add "gsc_property"' in out


def test_no_sites_file_exits_1_with_the_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "none.json"))
    code, out = run(capsys)
    assert code == 1
    assert "none.json" in out


def test_a_bad_sites_file_exits_1_without_its_values(write_config, capsys):
    write_config({"api_key": VALUE})
    code, out = run(capsys, "--json")
    assert code == 1
    assert json.loads(out)["ready"] is False
    assert VALUE not in out


def test_the_installed_command_runs_status(ready, tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "seo_mcp.server", "setup", "status"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert "www.example.com" in done.stdout
    assert VALUE not in done.stdout + done.stderr


NAMED = re.compile(r"site\.(field|secret|optional_secret)\(\"(\w+)\"\)")


@pytest.mark.parametrize(
    "module", [m for m in providers.MODULES if m.APIS], ids=lambda m: m.__name__
)
def test_status_knows_each_name_that_a_provider_reads(module):
    """A provider that reads a new field or secret must add it to setup.NEEDS, or status lies."""
    text = Path(module.__file__).read_text()
    if "_google." in text:
        text += (SOURCE / "_google.py").read_text()
    used = {name for _, name in NAMED.findall(text)}
    known = set().union(*(setup.NEEDS[api].names() for api in module.APIS))
    assert used == known, (
        f"{Path(module.__file__).name} reads {sorted(used)}, but setup.NEEDS for "
        f"{sorted(module.APIS)} names {sorted(known)}. Fix setup.NEEDS in src/seo_mcp/setup.py."
    )


def test_status_has_1_entry_for_each_api():
    assert set(setup.NEEDS) == set(providers.APIS)


PARITY = {
    "all ready": ({**FULL}, None),
    "no fields": ({}, None),
    "env not set": ({**FULL}, {"google_api_key": {"env": "SETUP_NO_SUCH_ENV"}}),
    "file missing": ({**FULL}, {"google_service_account": {"file": "/no/such/account.json"}}),
    "keychain missing": ({**FULL}, {"bing_api_key": {"keychain": "missing-item"}}),
    "no keychain tool": ({**FULL}, "no tool"),
    "no clarity token": ({**FULL}, {"clarity_token": None}),
}


@pytest.mark.parametrize("case", sorted(PARITY))
def test_status_and_list_sites_never_disagree(ready, write_config, keychain, monkeypatch, case):
    """The guard: setup status and the server read 1 function, so a ready provider is used."""
    fields, change = PARITY[case]
    path = config.path()
    body = json.loads(path.read_text())
    entry = next(iter(body["sites"].values()))
    for name in FULL:
        entry.pop(name, None)
    entry.update(fields)
    if change == "no tool":
        monkeypatch.setenv("PATH", "/nonexistent")
    elif case == "keychain missing":
        keychain(44)
        body["secrets"].update(change)
    elif change:
        for name, source in change.items():
            if source is None:
                entry["secrets"].pop(name)
            else:
                body["secrets"][name] = source
    path.write_text(json.dumps(body))
    (status_row,) = setup.status()["sites"]
    ready_now = {row["provider"]: row["state"] == "ready" for row in status_row["providers"]}
    (row,) = core.list_sites()["rows"]
    assert row["connected"] == ready_now, (
        f"{case}: setup status says {ready_now}, list_sites says {row['connected']}. Both must "
        "call connection.state in src/seo_mcp/connection.py."
    )
