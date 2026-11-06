import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from seo_mcp import config, net, secrets
from seo_mcp.providers import _google, clarity


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_MCP_CACHE_DIR", str(tmp_path / "cache"))
    # Each test has its own event loop, and a lock stays with the loop that it first waited in.
    monkeypatch.setattr(clarity, "_lock", asyncio.Lock())
    secrets.reset()
    yield
    net.transport = None
    secrets.reset()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def write_config(tmp_path, monkeypatch, repo):
    def write(site: dict | None = None, shared: dict | None = None) -> Path:
        entry = {"host": "www.example.com", **(site or {})}
        body = {"secrets": shared or {}, "sites": {str(repo): entry}}
        path = tmp_path / "sites.json"
        path.write_text(json.dumps(body))
        monkeypatch.setenv(config.CONFIG_ENV, str(path))
        return path

    return write


@pytest.fixture
def account(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "account.json"
    path.write_text(
        json.dumps(
            {"private_key": pem, "private_key_id": "fake-key-id", "client_email": "a@example.com"}
        )
    )
    _google._tokens.clear()
    return {"google_service_account": {"file": str(path)}}


@pytest.fixture
def fake_net():
    """Answer each request from a handler, through the real client code."""

    def install(handler):
        calls: list[httpx2.Request] = []

        def record(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return handler(request)

        net.transport = httpx2.MockTransport(record)
        return calls

    return install
