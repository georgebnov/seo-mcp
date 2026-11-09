"""The sites file. It lives outside the repo and holds only names, never a key value."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from seo_mcp import NAME, secrets
from seo_mcp.results import NotConnected

CONFIG_ENV = NAME.upper().replace("-", "_") + "_CONFIG"
DEFAULT_PATH = f"~/.config/{NAME}/sites.json"
SITE_FIELDS = {"host", "gsc_property", "ga4_property_id", "bing_site_url", "clarity_project"}
SOURCE_KINDS = ({"env"}, {"file"}, {"file", "json_key"}, {"keychain"})


class ConfigError(Exception):
    """The sites file cannot be used. The text never holds a field value."""


@dataclass(frozen=True)
class Site:
    host: str
    repo_path: Path
    fields: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    # connection.found fills it: a site loads again for each tool call, so each call probes again.
    probes: dict[str, bool | None] = field(default_factory=dict, compare=False, repr=False)

    def field(self, name: str) -> str:
        if name not in self.fields:
            raise NotConnected(f"The sites file has no {name} for {self.host}.")
        return self.fields[name]

    def secret(self, name: str) -> str:
        if name not in self.secrets:
            raise NotConnected(f"The sites file names no secret {name} for {self.host}.")
        return secrets.read(self.secrets[name])

    def optional_secret(self, name: str) -> str | None:
        return secrets.read(self.secrets[name]) if name in self.secrets else None


def path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or DEFAULT_PATH).expanduser()


def _check_secrets(entries: Any, where: str) -> dict[str, dict[str, str]]:
    if not isinstance(entries, dict):
        raise ConfigError(f'{where}: "secrets" must be an object.')
    for name, source in entries.items():
        valid = isinstance(source, dict) and set(source) in SOURCE_KINDS
        if not valid or not all(isinstance(item, str) for item in source.values()):
            raise ConfigError(
                f'{where}: secret {name} must name 1 of "env", "file", or "keychain".'
            )
    return entries


def _site(repo_path: str, entry: Any, shared: dict[str, dict[str, str]]) -> Site:
    if not isinstance(entry, dict) or not isinstance(entry.get("host"), str):
        raise ConfigError(f'The entry for {repo_path} needs a "host".')
    own = _check_secrets(entry.get("secrets", {}), repo_path)
    fields = {key: value for key, value in entry.items() if key != "secrets"}
    unknown = sorted(set(fields) - SITE_FIELDS)
    if unknown:
        raise ConfigError(f"The entry for {repo_path} has unknown fields: {', '.join(unknown)}.")
    for name, value in fields.items():
        if not isinstance(value, str) or secrets.scrub(value) != value:
            raise ConfigError(f"Field {name} of {repo_path} is not a plain name.")
    host = fields.pop("host").lower()
    return Site(host, Path(repo_path).expanduser().resolve(), fields, {**shared, **own})


def load() -> list[Site]:
    file = path()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotConnected(
            f"No sites file at {file}. Run {NAME} setup, or copy examples/sites.json there."
        ) from None
    except (OSError, ValueError):
        raise ConfigError(f"{file} is not readable JSON.") from None
    return parse(raw, file)


def parse(raw: Any, file: Path) -> list[Site]:
    """Check the JSON of a sites file. `seo-mcp setup` checks a new file with it before a write."""
    if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
        raise ConfigError(f'{file} must be an object with a "sites" object.')
    shared = _check_secrets(raw.get("secrets", {}), str(file))
    sites = [_site(repo, entry, shared) for repo, entry in raw["sites"].items()]
    hosts = [site.host for site in sites]
    if len(set(hosts)) != len(hosts):
        raise ConfigError(f"{file} has 2 entries for 1 host. Keep 1.")
    return sites


def _hosts(sites: list[Site]) -> str:
    return ", ".join(site.host for site in sites)


def resolve(site: str) -> Site:
    """Find a site by exact host or repo path. An empty value means the current folder."""
    sites = load()
    wanted = site.strip().lower()
    for entry in sites:
        if entry.host == wanted:
            return entry
    # A bare word is a mistyped host, never a path under the current folder.
    if wanted and not wanted.startswith(("/", "~")):
        raise ConfigError(f"No site matches {site}. Known sites: {_hosts(sites)}.")
    folder = Path(site or os.getcwd()).expanduser().resolve()
    matches = [entry for entry in sites if folder.is_relative_to(entry.repo_path)]
    if matches:
        return max(matches, key=lambda entry: len(entry.repo_path.parts))
    raise ConfigError(f"No site matches {site or folder}. Known sites: {_hosts(sites)}.")
