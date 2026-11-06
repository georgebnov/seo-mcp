import json

import pytest

from seo_mcp import config
from seo_mcp.results import NotConnected


def test_missing_file_is_not_connected(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "none.json"))
    with pytest.raises(NotConnected):
        config.load()


def test_one_new_entry_adds_a_site(write_config, tmp_path):
    path = write_config()
    body = json.loads(path.read_text())
    body["sites"]["/srv/two"] = {"host": "two.example.com"}
    path.write_text(json.dumps(body))
    assert sorted(site.host for site in config.load()) == ["two.example.com", "www.example.com"]


def test_site_by_host_ignores_case(write_config):
    write_config()
    assert config.resolve("WWW.example.com").host == "www.example.com"


def test_site_by_cwd_uses_path_parts(write_config, repo, monkeypatch):
    write_config()
    inner = repo / "src"
    inner.mkdir()
    monkeypatch.chdir(inner)
    assert config.resolve("").host == "www.example.com"
    twin = repo.parent / (repo.name + "-other")
    twin.mkdir()
    monkeypatch.chdir(twin)
    with pytest.raises(config.ConfigError):
        config.resolve("")


def test_unknown_host_never_guesses(write_config, repo, monkeypatch):
    write_config()
    monkeypatch.chdir(repo)
    with pytest.raises(config.ConfigError) as caught:
        config.resolve("example.com")
    assert "www.example.com" in str(caught.value)


def test_absolute_repo_path_selects_the_site(write_config, repo):
    write_config()
    assert config.resolve(str(repo / "src")).host == "www.example.com"


def test_unknown_field_is_refused_without_its_value(write_config):
    path = write_config({"api_key": "value-that-must-not-print"})
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert "api_key" in str(caught.value)
    assert "value-that-must-not-print" not in str(caught.value)
    assert path


def test_key_shaped_field_is_refused(write_config):
    write_config({"gsc_property": "AI" + "za" + "C" * 35})
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert "CCCC" not in str(caught.value)


def test_bad_secret_source_is_refused(write_config):
    write_config(shared={"bing_api_key": {"value": "x"}})
    with pytest.raises(config.ConfigError):
        config.load()


def test_duplicate_host_is_refused(write_config):
    path = write_config()
    body = json.loads(path.read_text())
    body["sites"]["/srv/two"] = {"host": "WWW.example.com"}
    path.write_text(json.dumps(body))
    with pytest.raises(config.ConfigError):
        config.load()


def test_site_secret_overrides_the_shared_one(write_config, monkeypatch):
    monkeypatch.setenv("SHARED", "shared-value-1")
    monkeypatch.setenv("OWN", "own-value-1")
    write_config({"secrets": {"t": {"env": "OWN"}}}, {"t": {"env": "SHARED"}})
    assert config.resolve("www.example.com").secret("t") == "own-value-1"


def test_missing_field_and_secret_are_not_connected(write_config):
    write_config()
    site = config.resolve("www.example.com")
    with pytest.raises(NotConnected):
        site.field("gsc_property")
    with pytest.raises(NotConnected):
        site.secret("bing_api_key")
    assert site.optional_secret("bing_api_key") is None
