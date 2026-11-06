"""Fail when the repo or its git history holds private data.

The generic shapes run everywhere. Literal private terms must not live in the repo, so
they come from the real sites file and an optional terms file, both outside the repo.
A finding names the place and the kind, and never prints the matched text.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONFIG_ENV = "SEO_MCP_CONFIG"
TERMS_ENV = "SEO_MCP_PRIVATE_TERMS"
CONFIG_PATH = "~/.config/seo-mcp/sites.json"
TERMS_PATH = "~/.config/seo-mcp/private-terms.txt"
MIN_TERM_LENGTH = 6
FIXTURE_ID = "123456789"
SKIP_FILES = {"uv.lock"}
PUBLIC_HOST = re.compile(
    r"(^|\.)(example\.com|test|googleapis\.com|google\.com|bing\.com|clarity\.ms"
    r"|microsoft\.com|github\.com|modelcontextprotocol\.io|astral\.sh)$"
)
SHAPES = {
    "home path": re.compile(r"/(Users|home)/[a-z]"),
    "Google API key": re.compile(r"AIza[\w-]{35}"),
    "Google token": re.compile(r"ya29\.[\w-]"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    "Search Console property": re.compile(r"sc-domain:(?!example\.com)[a-z0-9]"),
}
LONG_NUMBER = re.compile(r"\b\d{9,}\b")
URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def _site_terms(entry: dict[str, Any]) -> set[str]:
    terms = {value for key, value in entry.items() if key != "secrets"}
    terms |= {source.get("keychain", "") for source in entry.get("secrets", {}).values()}
    host = entry.get("host", "")
    return terms | {host.removeprefix("www.")}


def _is_term(term: str) -> bool:
    return len(term) >= MIN_TERM_LENGTH or (term.isalpha() and len(term) >= 3)


def private_terms() -> set[str]:
    terms: set[str] = set()
    config = Path(os.environ.get(CONFIG_ENV) or CONFIG_PATH).expanduser()
    if config.is_file():
        for repo, entry in json.loads(config.read_text())["sites"].items():
            terms |= {str(Path(repo).expanduser())} | _site_terms(entry)
    extra = Path(os.environ.get(TERMS_ENV) or TERMS_PATH).expanduser()
    if extra.is_file():
        lines = {line.strip() for line in extra.read_text().splitlines()}
        terms |= {line for line in lines if not line.startswith("#")}
    return {term.lower() for term in terms if _is_term(term)}


def _line_kinds(line: str, terms: set[str]) -> list[str]:
    kinds = [kind for kind, shape in SHAPES.items() if shape.search(line)]
    if any(number != FIXTURE_ID for number in LONG_NUMBER.findall(line)):
        kinds.append("long id")
    for url in URL.findall(line):
        host = urlsplit(url).hostname or ""
        if "{" not in url and not PUBLIC_HOST.search(host):
            kinds.append("unknown host")
    lowered = line.lower()
    if any(term in lowered for term in terms):
        kinds.append("private term")
    return kinds


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, errors="replace"
    )
    return done.stdout


def _scan_files(root: Path, terms: set[str]) -> list[str]:
    findings = []
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    for name in listed.splitlines():
        path = root / name
        if name in SKIP_FILES or path.is_symlink() or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            findings += [f"{name}:{number}: {kind}" for kind in _line_kinds(line, terms)]
    return findings


def _scan_history(root: Path, terms: set[str]) -> list[str]:
    findings = []
    commit = ""
    skip = False
    log = _git(root, "log", "--all", "-p", "--format=commit %H%n%s%n%b")
    for line in log.splitlines():
        if line.startswith("commit "):
            commit, skip = line.split()[1][:10], False
            continue
        if line.startswith("diff --git "):
            skip = line.rsplit("/", 1)[-1] in SKIP_FILES
        if not skip:
            findings += [f"history {commit}: {kind}" for kind in _line_kinds(line, terms)]
    return findings


def scan(root: Path, terms: set[str]) -> list[str]:
    return sorted(set(_scan_files(root, terms) + _scan_history(root, terms)))


def main() -> int:
    terms = private_terms()
    root = Path(__file__).resolve().parent.parent
    findings = scan(root, terms)
    for finding in findings:
        print(finding)
    commits = _git(root, "rev-list", "--all", "--count").strip()
    print(f"Scanned files and {commits} commits with {len(terms)} private terms.")
    print(f"{len(findings)} findings. Move each value to the sites file, outside the repo.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
