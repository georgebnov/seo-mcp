"""The `seo-mcp setup` terminal command. It is not an MCP tool.

`setup status` reads only the sites file. It makes no network call and reads no secret value: it
looks only for the env name, the file, or the Keychain item that a reference names.
"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from seo_mcp import NAME, config, secrets
from seo_mcp.config import ConfigError, Site
from seo_mcp.results import NotConnected

NONE = frozenset[str]()
READY = "ready"
MISSING_FIELD = "missing field"
MISSING_REFERENCE = "missing secret reference"
NOT_FOUND = "secret not found"
NOT_CHECKED = "not checked"
# The worst gap names the state of a provider.
ORDER = (MISSING_FIELD, MISSING_REFERENCE, NOT_FOUND, NOT_CHECKED)
KEYCHAIN_TIMEOUT_SECONDS = 10
KEYCHAIN_NO_ITEM = 44


class Needs(NamedTuple):
    fields: frozenset[str] = NONE
    secrets: frozenset[str] = NONE
    # A provider runs without these. Status checks one only when the sites file names it.
    optional: frozenset[str] = NONE

    def names(self) -> set[str]:
        return set(self.fields | self.secrets | self.optional)


NEEDS: dict[str, Needs] = {
    "search_console": Needs(frozenset({"gsc_property"}), frozenset({"google_service_account"})),
    "ga4": Needs(frozenset({"ga4_property_id"}), frozenset({"google_service_account"})),
    "pagespeed": Needs(optional=frozenset({"google_api_key"})),
    "crux": Needs(secrets=frozenset({"google_api_key"})),
    "bing": Needs(frozenset({"bing_site_url"}), frozenset({"bing_api_key"})),
    "clarity": Needs(secrets=frozenset({"clarity_token"})),
}

FIELD_HINTS = {
    "gsc_property": "the Search Console property, for example sc-domain:example.com",
    "ga4_property_id": "the GA4 property ID, for example 123456789",
    "bing_site_url": "the site URL in Bing Webmaster Tools, for example https://www.example.com/",
}


def _keychain_item_exists(item: str) -> bool | None:
    """Ask for the attributes of the item only. With no -w or -g, `security` gives no value."""
    tool = shutil.which("security")
    if not tool:
        return None
    try:
        done = subprocess.run(  # noqa: S603
            [tool, "find-generic-password", "-s", item],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if done.returncode == KEYCHAIN_NO_ITEM:
        return False
    return True if done.returncode == 0 else None


def _found(source: dict[str, str]) -> bool | None:
    if "env" in source:
        return source["env"] in os.environ
    if "file" in source:
        return Path(source["file"]).expanduser().is_file()
    return _keychain_item_exists(source["keychain"])


def _secret_gap(name: str, source: dict[str, str]) -> dict[str, str] | None:
    found = _found(source)
    if found:
        return None
    if "env" in source:
        step = (
            f"Set the environment variable {source['env']} in the environment that starts "
            f"the server, or change the reference of {name}."
        )
    elif "file" in source:
        step = f"Put the {name} file at {source['file']}, or change the reference of {name}."
    elif found is None:
        step = (
            f"Status cannot check the Keychain item {source['keychain']} on this machine. "
            "Make sure that the item exists."
        )
    else:
        step = (
            f"Add the Keychain item {source['keychain']} for {name}. "
            "The README Setup section shows the command."
        )
    return {"state": NOT_FOUND if found is False else NOT_CHECKED, "name": name, "step": step}


def _gaps(site: Site, needs: Needs, file: Path) -> list[dict[str, str]]:
    gaps = [
        {
            "state": MISSING_FIELD,
            "name": name,
            "step": f'Add "{name}" to the entry of {site.host} in {file}: {FIELD_HINTS[name]}.',
        }
        for name in sorted(needs.fields - set(site.fields))
    ]
    for name in sorted(needs.secrets | needs.optional):
        source = site.secrets.get(name)
        if source is None:
            if name in needs.secrets:
                gaps.append(
                    {
                        "state": MISSING_REFERENCE,
                        "name": name,
                        "step": (
                            f'Add "{name}" to the "secrets" object of {site.host}, or to the '
                            f'top-level "secrets" object of {file}. Name an env, a file, or '
                            "a Keychain item, never the value."
                        ),
                    }
                )
            continue
        gap = _secret_gap(name, source)
        if gap:
            gaps.append(gap)
    return gaps


def _provider(site: Site, api: str, file: Path) -> dict[str, Any]:
    gaps = _gaps(site, NEEDS[api], file)
    state = min((gap["state"] for gap in gaps), key=ORDER.index, default=READY)
    return {"provider": api, "state": state, "gaps": gaps}


def status(only: str | None = None) -> dict[str, Any]:
    """Check each site of the sites file, or only the site with the host `only`."""
    file = config.path()
    try:
        sites = config.load()
    except (NotConnected, ConfigError) as exc:
        return secrets.scrub({"file": str(file), "ready": False, "error": str(exc), "sites": []})
    rows = []
    for site in sites:
        if only is not None and site.host != only.lower():
            continue
        found = [_provider(site, api, file) for api in NEEDS]
        rows.append(
            {
                "host": site.host,
                "repo_path": str(site.repo_path),
                "ready": all(row["state"] == READY for row in found),
                "providers": found,
            }
        )
    return secrets.scrub(
        {"file": str(file), "ready": bool(rows) and all(r["ready"] for r in rows), "sites": rows}
    )


def render(report: dict[str, Any]) -> str:
    lines = [f"Sites file: {report['file']}"]
    if "error" in report:
        return "\n".join([*lines, report["error"]])
    width = max(len(api) for api in NEEDS)
    for site in report["sites"]:
        lines += ["", f"{site['host']}  ({site['repo_path']})"]
        for row in site["providers"]:
            lines.append(f"  {row['provider']:<{width}}  {row['state']}")
            lines += [f"    {gap['step']}" for gap in row["gaps"]]
    done = sum(site["ready"] for site in report["sites"])
    lines += ["", f"{done} of {len(report['sites'])} sites are ready."]
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=NAME, description="With no command, start the server.")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Set up the sites file.")
    steps = setup.add_subparsers(dest="step", required=True)
    check = steps.add_parser(
        "status", help="Show what each site needs. No network call, no secret value."
    )
    check.add_argument("--json", action="store_true", help="Print JSON for an agent.")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    report = status()
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ready"] else 1
