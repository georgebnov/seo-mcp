"""A write call must fail these tests, also in a fork.

The 1 write is submit_urls: providers/indexing.py sends URL_UPDATED to the Google Indexing API.
Only that file can name a write endpoint, the write host, or a scope that is not read-only.
"""

import ast
import re
from pathlib import Path

import pytest

from seo_mcp import providers
from seo_mcp.providers import indexing

SRC = Path(__file__).parent.parent / "src" / "seo_mcp"
# A name with a dot is 1 module of a package: urllib.parse opens no connection.
NETWORK_MODULES = {
    "httpx",
    "httpx2",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "socket",
    "http",
    "ssl",
    "ftplib",
    "smtplib",
    "xmlrpc",
}
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
    ("analyticsdata.googleapis.com", r"/v1beta/properties/\d+:runPivotReport"),
    ("analyticsdata.googleapis.com", r"/v1beta/properties/\d+:runRealtimeReport"),
    ("chromeuxreport.googleapis.com", r"/v1/records:queryRecord"),
    ("chromeuxreport.googleapis.com", r"/v1/records:queryHistoryRecord"),
    ("clarity.microsoft.com", r"/mcp/recordings/sample"),
    ("clarity.microsoft.com", r"/mcp/dashboard/query"),
}
# The only write. It lives in WRITER, and it spends from a ledger.
WRITES = {("indexing.googleapis.com", r"/v3/urlNotifications:publish")}
WRITER = SRC / "providers" / "indexing.py"
WRITER_TEXT = {"urlNotifications", "indexing.googleapis"}
WRITE_SCOPES = {"https://www.googleapis.com/auth/indexing"}
SCOPE = re.compile(re.escape("https://www.googleapis.com/auth/") + r"[\w.]+")
FIX = (
    "Only src/seo_mcp/providers/indexing.py, the submit_urls path, can name a write endpoint, "
    "the write host, or a write scope. Make the call a read, or change the rule in AGENTS.md."
)


def imported_modules(path: Path) -> set[str]:
    """Each imported name, as its package and as its full name."""
    full = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            full |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            full |= {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
    return full | {name.split(".")[0] for name in full}


def test_only_listed_posts_exist():
    posts = {(e.host, e.path) for e in providers.ENDPOINTS if e.method == "POST"}
    pinned = READ_POSTS | WRITES
    assert posts <= pinned, f"New POST endpoint. Read it, then pin it: {posts - pinned}"


def test_a_post_pin_has_an_endpoint():
    posts = {(e.host, e.path) for e in providers.ENDPOINTS if e.method == "POST"}
    pinned = READ_POSTS | WRITES
    assert pinned <= posts, f"A pin outlived its endpoint. Remove it: {pinned - posts}"


def test_only_the_writer_has_a_write_endpoint():
    for module in providers.MODULES:
        found = {(e.host, e.path) for e in module.ENDPOINTS}
        if module is indexing:
            assert found == WRITES, f"indexing.py has only the publish endpoint: {found}"
        else:
            assert not found & WRITES, f"{module.__name__} has a write endpoint. {FIX}"
            assert not {e.host for e in module.ENDPOINTS} & {h for h, _ in WRITES}, FIX


def test_the_write_spends_from_a_ledger():
    assert all(endpoint.metered for endpoint in indexing.ENDPOINTS)


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


@pytest.mark.parametrize(
    "line",
    [
        "import httpx2",
        "import http.client",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
    ],
)
def test_the_import_scan_finds_a_network_library(tmp_path, line):
    path = tmp_path / "module.py"
    path.write_text(f"{line}\nfrom urllib.parse import quote\n")
    assert imported_modules(path) & NETWORK_MODULES
    path.write_text("from urllib.parse import quote\nimport json\n")
    assert not imported_modules(path) & NETWORK_MODULES


def write_text(path: Path) -> list[str]:
    """The write words of 1 source file. The writer may name only its own endpoint."""
    found = [match.group() for match in WRITE_TEXT.finditer(path.read_text())]
    return [word for word in found if path != WRITER or word not in WRITER_TEXT]


def write_scopes(path: Path) -> list[str]:
    """The scopes of 1 source file that are not read-only. Only the writer may hold 1."""
    found = [scope for scope in SCOPE.findall(path.read_text()) if not scope.endswith(".readonly")]
    return [scope for scope in found if path != WRITER or scope not in WRITE_SCOPES]


def test_source_has_no_write_call_names():
    for path in SRC.rglob("*.py"):
        found = write_text(path)
        assert not found, f"{path.relative_to(SRC)} has write text {found}. {FIX}"


def test_only_the_writer_asks_for_a_write_scope():
    for path in SRC.rglob("*.py"):
        found = write_scopes(path)
        assert not found, f"{path.relative_to(SRC)} asks for the scope {found}. {FIX}"


def test_the_shared_sign_in_has_only_read_scopes():
    scopes = SCOPE.findall((SRC / "providers" / "_google.py").read_text())
    assert scopes
    assert all(scope.endswith(".readonly") for scope in scopes)


def test_the_writer_asks_for_the_indexing_scope_only():
    assert set(SCOPE.findall(WRITER.read_text())) == WRITE_SCOPES
