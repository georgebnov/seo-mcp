from pathlib import Path

from seo_mcp import config, providers

ROOT = Path(__file__).parent.parent


def test_example_file_loads(monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(ROOT / "examples" / "sites.json"))
    sites = config.load()
    assert [site.host for site in sites] == ["www.example.com", "blog.example.com"]
    assert set(sites[0].fields) == config.SITE_FIELDS - {"host"}


def test_readme_lists_every_source():
    readme = (ROOT / "README.md").read_text()
    assert all(f"`{name}`" in readme for name in providers.SOURCES)
