"""Whether a site is connected to a provider. The server and `setup status` both read this.

A site is connected to a provider only when the sites file has each field of the provider and
each secret reference points at a name that exists: an environment variable that is set, a file
that exists, or a Keychain item that exists. The check reads no secret value and makes no
network call. Anything missing means not connected.
"""

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import NamedTuple

from seo_mcp.config import Site
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
_probing = threading.Lock()


class Needs(NamedTuple):
    label: str
    fields: frozenset[str] = NONE
    secrets: frozenset[str] = NONE
    # A provider runs without these. A reference that the sites file names must still exist.
    optional: frozenset[str] = NONE

    def names(self) -> set[str]:
        return set(self.fields | self.secrets | self.optional)


NEEDS: dict[str, Needs] = {
    "search_console": Needs(
        "Search Console", frozenset({"gsc_property"}), frozenset({"google_service_account"})
    ),
    "ga4": Needs("GA4", frozenset({"ga4_property_id"}), frozenset({"google_service_account"})),
    "pagespeed": Needs("PageSpeed", optional=frozenset({"google_api_key"})),
    "crux": Needs("Chrome UX Report", secrets=frozenset({"google_api_key"})),
    "bing": Needs("Bing", frozenset({"bing_site_url"}), frozenset({"bing_api_key"})),
    "clarity": Needs("Clarity", secrets=frozenset({"clarity_token"})),
}


class Gap(NamedTuple):
    state: str
    name: str
    # The kind of the reference: env, file, or keychain. Empty for a field or no reference.
    kind: str = ""


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
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode == KEYCHAIN_NO_ITEM:
        return False
    return True if done.returncode == 0 else None


def _probe(source: dict[str, str]) -> bool | None:
    if "env" in source:
        return source["env"] in os.environ
    if "file" in source:
        return Path(source["file"]).expanduser().is_file()
    return _keychain_item_exists(source["keychain"])


def found(site: Site, source: dict[str, str]) -> bool | None:
    """True, False, or None (not checked). 1 site object is 1 tool call: it probes each once.

    The sources of 1 call check in parallel threads, so the lock lets 1 thread probe a name.
    """
    key = json.dumps(source, sort_keys=True)
    with _probing:
        if key not in site.probes:
            site.probes[key] = _probe(source)
        return site.probes[key]


def gaps(site: Site, provider: str) -> list[Gap]:
    needs = NEEDS[provider]
    result = [Gap(MISSING_FIELD, name) for name in sorted(needs.fields - set(site.fields))]
    for name in sorted(needs.secrets | needs.optional):
        source = site.secrets.get(name)
        if source is None:
            if name in needs.secrets:
                result.append(Gap(MISSING_REFERENCE, name))
            continue
        there = found(site, source)
        if not there:
            # A file reference can also hold json_key, in any order.
            kind = next(kind for kind in PLACES if kind in source)
            result.append(Gap(NOT_FOUND if there is False else NOT_CHECKED, name, kind))
    return result


def state(site: Site, provider: str) -> str:
    return min((gap.state for gap in gaps(site, provider)), key=ORDER.index, default=READY)


def connected(site: Site, provider: str) -> bool:
    return state(site, provider) == READY


PLACES = {"env": "environment variable", "file": "file", "keychain": "Keychain item"}
ABSENT = {"env": "is not set", "file": "does not exist", "keychain": "does not exist"}


def said(gap: Gap) -> str:
    """Fixed text with canonical names only. A reference name never shows: it can be a key."""
    if gap.state == MISSING_FIELD:
        return f"the sites file has no {gap.name}"
    if gap.state == MISSING_REFERENCE:
        return f"{gap.name} has no secret reference"
    if gap.state == NOT_FOUND:
        return f"the {PLACES[gap.kind]} that {gap.name} names {ABSENT[gap.kind]}"
    return f"seo-mcp cannot check the {PLACES[gap.kind]} that {gap.name} names on this machine"


def missing(site: Site, provider: str) -> list[str]:
    return [said(gap) for gap in gaps(site, provider)]


def reason(site: Site, provider: str) -> str:
    label = NEEDS[provider].label
    parts = ", and ".join(missing(site, provider))
    return f"This site has no {label} connection: {parts}, so seo-mcp has no {label} data for it."


def require(site: Site, provider: str) -> None:
    """Stop before the provider runs: it then reads no secret, no cache, and no ledger."""
    if not connected(site, provider):
        raise NotConnected(reason(site, provider))
