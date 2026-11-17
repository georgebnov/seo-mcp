from pathlib import Path

from seo_mcp import config, providers

ROOT = Path(__file__).parent.parent


def test_example_file_loads(monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(ROOT / "examples" / "sites.json"))
    sites = config.load()
    assert [site.host for site in sites] == ["www.example.com", "blog.example.com"]
    # A user copies this file, so it shows each field but the opt-in of the 1 write.
    assert set(sites[0].fields) == config.SITE_FIELDS - {"host", config.OPT_IN}
    opted_in = [site.host for site in sites if config.OPT_IN in site.fields]
    assert not opted_in, (
        f"examples/sites.json turns on submit_urls for {opted_in}. Remove the field: only the "
        "user turns on the write."
    )


def test_readme_lists_every_source():
    readme = (ROOT / "README.md").read_text()
    assert all(f"`{name}`" in readme for name in providers.SOURCES)
