import json
import os
import stat

import pytest

from seo_mcp import secrets
from seo_mcp.results import NotConnected

KEY = "plain-secret-value-1234"


def test_env_source(monkeypatch):
    monkeypatch.setenv("MY_KEY", KEY)
    assert secrets.read({"env": "MY_KEY"}) == KEY


def test_file_source_and_json_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"api_key": KEY}))
    assert secrets.read({"file": str(path), "json_key": "api_key"}) == KEY
    assert json.loads(secrets.read({"file": str(path)})) == {"api_key": KEY}


def test_keychain_source_uses_security_tool(tmp_path, monkeypatch):
    tool = tmp_path / "security"
    tool.write_text(f"#!/bin/sh\necho {KEY}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert secrets.read({"keychain": "item"}) == KEY


def test_keychain_failure_hides_tool_output(tmp_path, monkeypatch):
    tool = tmp_path / "security"
    tool.write_text(f"#!/bin/sh\necho {KEY} >&2\nexit 44\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(NotConnected) as caught:
        secrets.read({"keychain": "item"})
    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    "source",
    [{"env": "NOT_SET_ANYWHERE"}, {"file": "/nonexistent/file"}],
)
def test_missing_source_is_not_connected(source):
    with pytest.raises(NotConnected):
        secrets.read(source)


def test_bad_json_file_does_not_echo_content(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(KEY)
    with pytest.raises(NotConnected) as caught:
        secrets.read({"file": str(path), "json_key": "api_key"})
    assert KEY not in str(caught.value)


def test_scrub_removes_a_read_value_in_keys_values_and_encodings(monkeypatch):
    value = "abc/def+ghi secret"
    monkeypatch.setenv("MY_KEY", value)
    secrets.read({"env": "MY_KEY"})
    dirty = {
        value: [value, "https://x.test/?a=abc%2Fdef%2Bghi%20secret", "abc%2Fdef%2Bghi+secret"],
    }
    assert "abc" not in json.dumps(secrets.scrub(dirty))


@pytest.mark.parametrize(
    "text",
    [
        "AI" + "za" + "B" * 35,
        "ya" + "29." + "tokenbody",
        "url?apikey=abc123&x=1",
        "url?key=abc123",
        "Authorization: Bearer abc.def",
        "-----BEGIN " + "PRIVATE KEY-----\nbody\n-----END " + "PRIVATE KEY-----",
    ],
)
def test_scrub_removes_key_shapes(text):
    cleaned = secrets.scrub(text)
    for part in ("BBBBB", "tokenbody", "abc123", "abc.def", "body"):
        assert part not in cleaned


def test_scrub_runs_before_the_cut():
    pem = "-----BEGIN " + "PRIVATE KEY-----\n" + "x" * 500 + "\n-----END " + "PRIVATE KEY-----"
    reason = secrets.safe_reason("a" * 390 + pem)
    assert "xxx" not in reason
    assert "BEGIN" not in reason
