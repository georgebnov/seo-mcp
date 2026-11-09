"""The `seo-mcp setup` terminal command. It is not an MCP tool.

`setup status` reads only the sites file. It makes no network call and reads no secret value: it
looks only for the env name, the file, or the Keychain item that a reference names.

`setup` asks for 1 site and writes the sites file. It asks where each secret lives, never the
value. It writes under a lock with an atomic replace, so a write that stops keeps the old file.
"""

import argparse
import asyncio
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from seo_mcp import NAME, config, quota, secrets
from seo_mcp.config import ConfigError, Site
from seo_mcp.results import NotConnected, Refused

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
    "clarity_project": "the Clarity project, for example example-project (with none, the host)",
}
SECRET_HINTS = {
    "google_service_account": "the JSON key of the service account, for Search Console and GA4",
    "google_api_key": "the Google API key, for PageSpeed and the Chrome UX Report",
    "bing_api_key": "the Bing Webmaster Tools API key",
    "clarity_token": "the Data Export token of the Clarity project of this site",
}
REMOVE = "-"
KINDS = ("env", "file", "keychain")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A reference is a short name. A longer answer is most likely a pasted key.
NAME_LIMIT = 200
YES = ("y", "yes")


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
            "step": (
                f'Add "{name}" to the entry of {site.host} in {file}: {FIELD_HINTS[name]}. '
                f"Or run {NAME} setup."
            ),
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
    setup = commands.add_parser(
        "setup", help="Add or change 1 site in the sites file. It asks no key value."
    )
    steps = setup.add_subparsers(dest="step")
    check = steps.add_parser(
        "status", help="Show what each site needs. No network call, no secret value."
    )
    check.add_argument("--json", action="store_true", help="Print JSON for an agent.")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.step is None:
        try:
            return write_site()
        except KeyboardInterrupt:
            print("\nStopped. The sites file did not change.")
            return 130
    report = status()
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ready"] else 1


class Changed(Exception):
    """Another process wrote the sites file after this command read it."""


def _ask(prompt: str) -> str:
    """An empty answer keeps the value. At the end of the input, each answer is empty."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _ask_until(prompt: str, problem: Callable[[str], str | None]) -> str:
    """Ask again after a bad answer. The reason never repeats the answer: it can be a key."""
    while True:
        answer = _ask(prompt)
        reason = problem(answer) if answer and answer != REMOVE else None
        if not reason:
            return answer
        print(reason)


def _plain(answer: str) -> str | None:
    if len(answer) > NAME_LIMIT or secrets.scrub(answer) != answer:
        return "That looks like a key value. Type only a name, never the key."
    return None


def _host_problem(answer: str) -> str | None:
    if re.search(r"[/:\s]", answer):
        return "Type the host only, for example www.example.com."
    return _plain(answer)


def _env_problem(answer: str) -> str | None:
    if not ENV_NAME.fullmatch(answer):
        return "An environment variable name has only letters, digits, and _."
    return _plain(answer)


def _kind_problem(answer: str) -> str | None:
    return None if answer in KINDS else f"Type 1 of {', '.join(KINDS)}."


def _describe(source: dict[str, str]) -> str:
    return ", ".join(f"{kind} {value}" for kind, value in source.items())


def _ask_secret(name: str, own: dict[str, Any], shared: dict[str, Any]) -> None:
    if name in own:
        now = _describe(own[name])
    elif name in shared:
        now = f"{_describe(shared[name])}, for all sites"
    else:
        now = "none"
    print(f"\n{name}: {SECRET_HINTS[name]} (now: {now}).")
    kind = _ask_until(
        f"Where does it live? Type {', '.join(KINDS)}, or {REMOVE} to remove it from this site: ",
        _kind_problem,
    )
    if kind == REMOVE:
        own.pop(name, None)
        return
    if not kind:
        return
    label = {"env": "Environment variable name", "file": "File path", "keychain": "Keychain item"}
    value = _ask_until(f"{label[kind]}: ", _env_problem if kind == "env" else _plain)
    if not value or value == REMOVE:
        return
    source = {kind: value}
    if kind == "file":
        field = _ask_until("JSON field in the file (empty if the file is the key): ", _plain)
        if field and field != REMOVE:
            source["json_key"] = field
    own[name] = source


def _find(sites: dict[str, Any], host: str) -> str | None:
    for repo, entry in sites.items():
        if isinstance(entry, dict) and str(entry.get("host", "")).lower() == host:
            return repo
    return None


def _folder(repo: str) -> Path:
    return Path(repo).expanduser().resolve()


def _ask_entry(sites: dict[str, Any], shared: dict[str, Any], host: str) -> dict[str, Any] | None:
    """Ask for the entry of 1 host. None means no repo path, so nothing changes."""
    old_repo = _find(sites, host)
    entry: dict[str, Any] = copy.deepcopy(sites[old_repo]) if old_repo else {"host": host}
    # 2 keys can name 1 folder, such as ~/code/site and its full path.
    taken = {_folder(key) for key in sites if key != old_repo}

    def repo_problem(answer: str) -> str | None:
        if _folder(answer) in taken:
            return "That repo path belongs to another site. Type another path."
        return _plain(answer)

    default: str | None = old_repo or os.getcwd()
    if default and repo_problem(default):
        print("The current folder is the repo path of another site.")
        default = None
    typed = _ask_until(f"Repo path [{default or 'none'}]: ", repo_problem)
    repo = default if not typed or typed == REMOVE else typed
    if repo is None:
        return None
    # The server resolves a relative path from its own folder, not from this one.
    if repo == typed and not repo.startswith("~") and not Path(repo).is_absolute():
        repo = str(_folder(repo))
    for name, hint in FIELD_HINTS.items():
        value = _ask_until(f"{name}: {hint} (now: {entry.get(name, 'none')}): ", _plain)
        if value == REMOVE:
            entry.pop(name, None)
        elif value:
            entry[name] = value
    own: dict[str, Any] = dict(entry.get("secrets", {}))
    for name in SECRET_HINTS:
        _ask_secret(name, own, shared)
    entry.pop("secrets", None)
    if own:
        entry["secrets"] = own
    # A new repo path keeps the place of the entry in the file.
    new_sites = {(repo if key == old_repo else key): value for key, value in sites.items()}
    new_sites[repo] = entry
    return new_sites


def _replace(target: Path, text: str) -> None:
    """Write a new file next to the old one, then rename it over the old one in 1 step."""
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    temp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        temp.chmod(mode)
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


async def _write(target: Path, old: str | None, new: str) -> None:
    async with quota.locked(target):
        now = target.read_text(encoding="utf-8") if target.exists() else None
        if now != old:
            raise Changed
        _replace(target, new)


def _read(file: Path, target: Path) -> tuple[str | None, dict[str, Any]]:
    if not target.exists():
        return None, {"sites": {}}
    try:
        old = target.read_text(encoding="utf-8")
        raw = json.loads(old)
    except (OSError, ValueError):
        raise ConfigError(f"{file} is not readable JSON.") from None
    config.parse(raw, file)
    return old, raw


def _save(file: Path, target: Path, old: str | None, new: str) -> str | None:
    """Write the new text. Give the reason when the file did not change."""
    try:
        asyncio.run(_write(target, old, new))
    except Refused as exc:
        return f"{exc} The sites file did not change."
    except Changed:
        return f"{file} changed while you answered, so it did not change. Run {NAME} setup again."
    except OSError:
        return f"The write stopped, so the old file is kept: {file}."
    return None


def write_site() -> int:
    file = config.path()
    # A link to the file, such as one into a dotfiles repo, stays a link.
    target = file.resolve()
    try:
        old, raw = _read(file, target)
    except ConfigError as exc:
        print(f"{secrets.scrub(str(exc))} Fix it, then run {NAME} setup again.")
        return 1
    print(
        f"Sites file: {file}\n"
        "Press Return to keep a value, or type - to remove it. For a secret, type where it "
        "lives, never the key value."
    )
    host = _ask_until("Host, for example www.example.com: ", _host_problem).lower()
    if not host or host == REMOVE:
        print("No host, so the sites file did not change.")
        return 0
    sites = _ask_entry(raw["sites"], raw.get("secrets", {}), host)
    if sites is None:
        print("No repo path, so the sites file did not change.")
        return 0
    new_raw = {**raw, "sites": sites}
    try:
        config.parse(new_raw, file)
    except ConfigError as exc:
        print(f"{secrets.scrub(str(exc))} The sites file did not change.")
        return 1
    new = json.dumps(new_raw, indent=2) + "\n"
    if new == old:
        print("\nNo change.")
    else:
        diff = difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(file),
            tofile=f"{file} (new)",
        )
        print("\n" + "".join(diff))
        if _ask(f"Write this change to {file}? Type y to write: ").lower() not in YES:
            print("The sites file did not change.")
        else:
            failed = _save(file, target, old, new)
            if failed:
                print(failed)
                return 1
            print(f"Wrote {file}.")
    report = status(host)
    print("\n" + render(report))
    return 0 if report["ready"] else 1
