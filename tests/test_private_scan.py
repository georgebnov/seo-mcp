import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "private_scan.py"
spec = importlib.util.spec_from_file_location("private_scan", SCRIPT)
assert spec and spec.loader
scan = importlib.util.module_from_spec(spec)
sys.modules["private_scan"] = scan
spec.loader.exec_module(scan)


def git(repo: Path, *args: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", *identity, *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "scanned"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "ok.txt").write_text("see https://www.example.com/ and id 123456789\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "chore: add file")
    return repo


def test_clean_repo_has_no_findings(clean_repo):
    assert scan.scan(clean_repo, set()) == []


@pytest.mark.parametrize(
    "text",
    [
        "/" + "Users/someone/x",
        "/" + "home/someone/x",
        "AI" + "za" + "D" * 35,
        "ya" + "29.abcdefgh",
        "-----BEGIN " + "PRIVATE KEY-----",
        "property 9876" + "54321",
        "sc-" + "domain:real-site.org",
        "https://" + "real-site.org/page",
    ],
)
def test_each_shape_fires_in_a_file_and_names_it(clean_repo, text):
    (clean_repo / "leak.txt").write_text(text + "\n")
    git(clean_repo, "add", "-A")
    findings = scan.scan(clean_repo, set())
    assert any("leak.txt" in finding for finding in findings), findings


def test_a_removed_leak_still_fires_from_history(clean_repo):
    leak = clean_repo / "leak.txt"
    leak.write_text("AI" + "za" + "E" * 35 + "\n")
    git(clean_repo, "add", "-A")
    git(clean_repo, "commit", "-qm", "chore: add leak")
    leak.unlink()
    git(clean_repo, "add", "-A")
    git(clean_repo, "commit", "-qm", "chore: remove leak")
    findings = scan.scan(clean_repo, set())
    assert any("history" in finding for finding in findings), findings


def test_a_commit_message_is_scanned(clean_repo):
    git(clean_repo, "commit", "-q", "--allow-empty", "-m", "fix: hide acme-private-term")
    assert scan.scan(clean_repo, {"acme-private-term"})


def test_private_terms_fire_and_do_not_print_in_git_tracked_text(clean_repo):
    (clean_repo / "leak.txt").write_text("the Acme-Private-Term here\n")
    git(clean_repo, "add", "-A")
    assert scan.scan(clean_repo, {"acme-private-term"})


def test_terms_come_from_the_real_config_and_the_terms_file(tmp_path, monkeypatch):
    config = tmp_path / "sites.json"
    site = {"host": "www.real-site.org", "ga4_property_id": "42", "secrets": {"t": {"env": "X"}}}
    config.write_text(json.dumps({"sites": {"~/code/real": site}}))
    terms = tmp_path / "terms.txt"
    terms.write_text("# a comment\nacme\n\n")
    monkeypatch.setenv("SEO_MCP_CONFIG", str(config))
    monkeypatch.setenv("SEO_MCP_PRIVATE_TERMS", str(terms))
    found = scan.private_terms()
    assert {"www.real-site.org", "acme"} <= found
    assert "42" not in found


def test_this_repo_is_clean():
    assert scan.scan(ROOT, scan.private_terms()) == []
