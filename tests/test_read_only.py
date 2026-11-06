"""A write call must fail these tests, also in a fork."""

import ast
import re
from pathlib import Path

from seo_mcp import providers

SRC = Path(__file__).parent.parent / "src" / "seo_mcp"
NETWORK_MODULES = {"httpx", "httpx2", "requests", "aiohttp", "urllib3", "socket", "http", "ssl"}
WRITE_TEXT = re.compile(
    r"Submit[A-Z]|Add[A-Z]|Remove[A-Z]|Delete[A-Z]|Update[A-Z]|urlNotifications|indexing\.googleapis"
    r'|"(PUT|PATCH|DELETE)"'
)

# Each POST here is a read: the API takes its query in the body. A new POST must be
# read and added here on purpose.
READ_POSTS = {
    ("oauth2.googleapis.com", r"/token"),
    ("searchconsole.googleapis.com", r"/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    ("searchconsole.googleapis.com", r"/v1/urlInspection/index:inspect"),
    ("analyticsdata.googleapis.com", r"/v1beta/properties/\d+:runReport"),
    ("chromeuxreport.googleapis.com", r"/v1/records:queryRecord"),
}


def imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_listed_posts_exist():
    posts = {(e.host, e.path) for e in providers.ENDPOINTS if e.method == "POST"}
    assert posts <= READ_POSTS, f"New POST endpoint. Read it, then pin it: {posts - READ_POSTS}"


def test_every_other_endpoint_is_get():
    assert {e.method for e in providers.ENDPOINTS} <= {"GET", "POST"}


def test_bing_endpoints_are_get_calls():
    for endpoint in providers.ENDPOINTS:
        if "bing" in endpoint.host:
            assert re.search(r"/Get[A-Za-z]+$", endpoint.path), endpoint


def test_only_net_module_imports_a_network_library():
    for path in SRC.rglob("*.py"):
        if path.name != "net.py":
            found = imported_modules(path) & NETWORK_MODULES
            assert not found, f"{path.name} imports {found}. Use seo_mcp.net."


def test_source_has_no_write_call_names():
    for path in SRC.rglob("*.py"):
        match = WRITE_TEXT.search(path.read_text())
        assert not match, f"{path.name} has write text {match.group()!r}. This server is read-only."


def test_oauth_scopes_are_read_only():
    text = (SRC / "providers" / "_google.py").read_text()
    scopes = re.findall(r"https://www.googleapis.com/auth/[\w.]+", text)
    assert scopes
    assert all(scope.endswith(".readonly") for scope in scopes)
