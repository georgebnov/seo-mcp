"""`seo-mcp setup`: an interactive writer of the sites file that never asks for a key value."""

import fcntl
import io
import json
import resource
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from seo_mcp import config, quota, server, setup

ROOT = Path(__file__).parent.parent
VALUE = "secret-value-that-must-not-print-1234"
SKIP_FIELDS = ["", "", "", ""]
SKIP_SECRETS = ["", "", "", ""]


def answers(*lines: str) -> io.StringIO:
    return io.StringIO("".join(f"{line}\n" for line in lines))


def run(monkeypatch, capsys, stdin: io.StringIO) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", stdin)
    with pytest.raises(SystemExit) as done:
        server.main(["setup"])
    out = capsys.readouterr()
    return int(done.value.code or 0), out.out + out.err


@pytest.fixture
def sites_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "sites.json"
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    return path


@pytest.fixture
def two_sites(sites_file, tmp_path):
    body = {
        "secrets": {"google_service_account": {"file": str(tmp_path / "account.json")}},
        "sites": {
            "/srv/one": {
                "host": "one.example.com",
                "gsc_property": "https://one.example.com/",
                "secrets": {"clarity_token": {"keychain": "clarity-one"}},
            },
            "/srv/two": {"host": "two.example.com", "bing_site_url": "https://two.example.com/"},
        },
    }
    sites_file.parent.mkdir()
    sites_file.write_text(json.dumps(body, indent=4))
    return body


def test_a_new_site_holds_only_references(sites_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SETUP_GOOGLE_KEY", VALUE)
    account = tmp_path / "account.json"
    account.write_text(VALUE)
    stdin = answers(
        "www.example.com",
        "/srv/site",
        "sc-domain:example.com",
        "123456789",
        "",
        "",
        "file",
        str(account),
        "",
        "env",
        "SETUP_GOOGLE_KEY",
        "",
        "",
        "y",
    )
    code, out = run(monkeypatch, capsys, stdin)
    text = sites_file.read_text()
    assert VALUE not in text and VALUE not in out
    assert json.loads(text) == {
        "sites": {
            "/srv/site": {
                "host": "www.example.com",
                "gsc_property": "sc-domain:example.com",
                "ga4_property_id": "123456789",
                "secrets": {
                    "google_service_account": {"file": str(account)},
                    "google_api_key": {"env": "SETUP_GOOGLE_KEY"},
                },
            }
        }
    }
    assert sites_file.stat().st_mode & 0o777 == 0o600
    assert '+      "gsc_property": "sc-domain:example.com",' in out
    assert "search_console  ready" in out
    assert "bing            missing field" in out
    assert code == 1
    assert [site.host for site in config.load()] == ["www.example.com"]


def test_other_sites_and_fields_stay(two_sites, sites_file, monkeypatch, capsys):
    stdin = answers("two.example.com", "", "", "12345678", "-", "", *SKIP_SECRETS, "y")
    code, out = run(monkeypatch, capsys, stdin)
    after = json.loads(sites_file.read_text())
    assert after["secrets"] == two_sites["secrets"]
    assert after["sites"]["/srv/one"] == two_sites["sites"]["/srv/one"]
    assert after["sites"]["/srv/two"] == {"host": "two.example.com", "ga4_property_id": "12345678"}
    assert list(after["sites"]) == ["/srv/one", "/srv/two"]
    status = out[out.rindex("Sites file:") :]
    assert "two.example.com" in status and "one.example.com" not in status


def test_a_new_repo_path_keeps_the_place_of_the_entry(two_sites, sites_file, monkeypatch, capsys):
    stdin = answers("one.example.com", "/srv/moved", *SKIP_FIELDS, *SKIP_SECRETS, "y")
    run(monkeypatch, capsys, stdin)
    after = json.loads(sites_file.read_text())
    assert list(after["sites"]) == ["/srv/moved", "/srv/two"]
    assert after["sites"]["/srv/moved"] == two_sites["sites"]["/srv/one"]


@pytest.mark.parametrize("typed", ["", "{repo}", "{repo}/"])
def test_a_new_site_never_takes_the_repo_path_of_another(
    sites_file, tmp_path, monkeypatch, capsys, typed
):
    """The current folder is the default repo path, and it can be the repo of another site."""
    repo = tmp_path / "one"
    repo.mkdir()
    sites_file.parent.mkdir()
    body = {"sites": {str(repo): {"host": "one.example.com"}}}
    sites_file.write_text(json.dumps(body))
    before = sites_file.read_bytes()
    monkeypatch.chdir(repo)
    stdin = answers("new.example.com", typed.format(repo=repo), *SKIP_FIELDS, *SKIP_SECRETS, "y")
    code, out = run(monkeypatch, capsys, stdin)
    assert sites_file.read_bytes() == before
    assert "another site" in out


def test_a_relative_repo_path_is_stored_in_full(sites_file, tmp_path, monkeypatch, capsys):
    """The server resolves a repo path from its own folder, so "." would name another folder."""
    repo = tmp_path / "site"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    run(monkeypatch, capsys, answers("www.example.com", "site", *SKIP_FIELDS, *SKIP_SECRETS, "y"))
    assert list(json.loads(sites_file.read_text())["sites"]) == [str(repo.resolve())]


def test_a_kept_relative_repo_path_stays_as_it_is(sites_file, monkeypatch, capsys):
    sites_file.parent.mkdir()
    sites_file.write_text(json.dumps({"sites": {"code/one": {"host": "one.example.com"}}}))
    stdin = answers("one.example.com", "", "", "12345678", "", "", *SKIP_SECRETS, "y")
    run(monkeypatch, capsys, stdin)
    assert list(json.loads(sites_file.read_text())["sites"]) == ["code/one"]


def test_the_examples_file_still_loads(sites_file, monkeypatch, capsys):
    example = ROOT / "examples" / "sites.json"
    original = example.read_bytes()
    sites_file.parent.mkdir()
    shutil.copy(example, sites_file)
    stdin = answers("blog.example.com", "", "", "87654321", "", "", *SKIP_SECRETS, "y")
    run(monkeypatch, capsys, stdin)
    before = json.loads(original)
    after = json.loads(sites_file.read_text())
    assert example.read_bytes() == original
    assert after["secrets"] == before["secrets"]
    assert after["sites"]["~/code/example"] == before["sites"]["~/code/example"]
    assert after["sites"]["~/code/example-blog"]["ga4_property_id"] == "87654321"
    assert [site.host for site in config.load()] == ["www.example.com", "blog.example.com"]


@pytest.mark.parametrize("confirm", ["n", None])
def test_no_yes_keeps_the_file(two_sites, sites_file, monkeypatch, capsys, confirm):
    before = sites_file.read_bytes()
    lines = ["two.example.com", "", "", "12345678", "", "", *SKIP_SECRETS]
    run(monkeypatch, capsys, answers(*lines, *([confirm] if confirm else [])))
    assert sites_file.read_bytes() == before


def test_a_stopped_write_keeps_the_old_file(two_sites, sites_file):
    """The kernel stops the write of the new file at a size limit, part way through."""
    before = sites_file.read_bytes()
    limit = 64

    def small_files() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    stdin = "\n".join(["two.example.com", "", "https://two.example.com/", "12345678"])
    stdin += "\n" + "\n".join(["", "a-long-clarity-project-name", *SKIP_SECRETS, "y"]) + "\n"
    done = subprocess.run(
        [sys.executable, "-m", "seo_mcp.server", "setup"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        preexec_fn=small_files,
    )
    assert done.returncode != 0
    assert sites_file.read_bytes() == before
    assert sorted(p.name for p in sites_file.parent.iterdir()) == ["sites.json", "sites.lock"]
    assert "old file" in done.stdout + done.stderr


def test_a_held_lock_writes_nothing(two_sites, sites_file, monkeypatch, capsys):
    monkeypatch.setattr(quota, "LOCK_WAIT_SECONDS", 0.2)
    before = sites_file.read_bytes()
    lock = sites_file.with_suffix(".lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    timer = threading.Timer(1.0, lock.close)
    timer.start()
    stdin = answers("two.example.com", "", "", "12345678", "", "", *SKIP_SECRETS, "y")
    code, out = run(monkeypatch, capsys, stdin)
    timer.join()
    assert code == 1
    assert "busy" in out
    assert sites_file.read_bytes() == before


class ChangesTheFile(io.StringIO):
    """Another process edits the file while the user reads the diff."""

    def __init__(self, text: str, path: Path) -> None:
        super().__init__(text)
        self.path = path
        self.count = 0

    def readline(self, *args):
        self.count += 1
        if self.count == 11:
            self.path.write_text(self.path.read_text().replace("two.example.com/", "x/"))
        return super().readline(*args)


def test_a_file_that_changed_meanwhile_is_not_overwritten(
    two_sites, sites_file, monkeypatch, capsys
):
    lines = ["two.example.com", "", "", "12345678", "", "", *SKIP_SECRETS, "y"]
    code, out = run(monkeypatch, capsys, ChangesTheFile("\n".join(lines) + "\n", sites_file))
    assert code == 1
    assert "changed" in out
    assert "ga4_property_id" not in sites_file.read_text()


@pytest.mark.parametrize(
    "pasted",
    ["ya29" + ".a0AfH6SMBexampleexampleexample", "x" * 300, "Bearer abcdefghijklmnop"],
)
def test_a_pasted_key_is_refused_and_never_shown(sites_file, monkeypatch, capsys, pasted):
    stdin = answers(
        "www.example.com",
        "/srv/site",
        *SKIP_FIELDS,
        "",
        "",
        "",
        "keychain",
        pasted,
        "clarity-item",
        "y",
    )
    code, out = run(monkeypatch, capsys, stdin)
    text = sites_file.read_text()
    assert pasted not in text and pasted not in out
    assert json.loads(text)["sites"]["/srv/site"]["secrets"] == {
        "clarity_token": {"keychain": "clarity-item"}
    }


def test_a_url_as_host_is_asked_again(sites_file, monkeypatch, capsys):
    stdin = answers(
        "https://www.example.com/", "www.example.com", "/srv/site", *SKIP_FIELDS, *SKIP_SECRETS, "y"
    )
    run(monkeypatch, capsys, stdin)
    assert config.load()[0].host == "www.example.com"


def test_no_host_changes_nothing(sites_file, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, answers(""))
    assert code == 0
    assert not sites_file.exists()


def test_a_bad_file_is_not_edited(sites_file, monkeypatch, capsys):
    sites_file.parent.mkdir()
    sites_file.write_text('{"sites": {"/srv/a": {"host": "a.example.com", "api_key": "v"}}}')
    before = sites_file.read_bytes()
    code, out = run(monkeypatch, capsys, answers("a.example.com"))
    assert code == 1
    assert sites_file.read_bytes() == before


def test_a_linked_file_stays_a_link(two_sites, sites_file, tmp_path, monkeypatch, capsys):
    real = tmp_path / "dotfiles" / "sites.json"
    real.parent.mkdir()
    sites_file.replace(real)
    sites_file.symlink_to(real)
    stdin = answers("two.example.com", "", "", "12345678", "", "", *SKIP_SECRETS, "y")
    run(monkeypatch, capsys, stdin)
    assert sites_file.is_symlink()
    assert json.loads(real.read_text())["sites"]["/srv/two"]["ga4_property_id"] == "12345678"


def test_setup_asks_for_each_field_and_secret():
    """A new site field or provider secret must get a question in setup, or setup cannot set it."""
    secret_names = set().union(*(needs.secrets | needs.optional for needs in setup.NEEDS.values()))
    fields = config.SITE_FIELDS - {"host"}
    assert set(setup.FIELD_HINTS) == fields, (
        f"setup.FIELD_HINTS asks {sorted(setup.FIELD_HINTS)}, but the sites file has "
        f"{sorted(fields)}. Fix FIELD_HINTS in src/seo_mcp/setup.py."
    )
    assert set(setup.SECRET_HINTS) == secret_names, (
        f"setup.SECRET_HINTS asks {sorted(setup.SECRET_HINTS)}, but the providers read "
        f"{sorted(secret_names)}. Fix SECRET_HINTS in src/seo_mcp/setup.py."
    )
