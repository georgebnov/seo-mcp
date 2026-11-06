"""Read secrets from where the config says they live, and keep them out of all output."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus

from seo_mcp.results import NotConnected

REASON_LIMIT = 400
MIN_SECRET_LENGTH = 8
KEYCHAIN_TIMEOUT_SECONDS = 10

# Backstop only. Prevention is that a reason is fixed text.
SECRET_SHAPES = (
    (
        re.compile(r"(?i)\b(api[_-]?key|key|access_token|refresh_token|token)=[^&\s'\"<>)]+"),
        r"\1=*",
    ),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer *"),
    (re.compile(r"\bya29\.[\w.-]+"), "*"),
    (re.compile(r"\bAIza[\w-]{35}"), "*"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "*",
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.S), "*"),
)

_known: set[str] = set()


def remember(value: str) -> None:
    """Add a value, in each encoding that a URL or JSON can give it, to the scrub list."""
    if len(value) >= MIN_SECRET_LENGTH:
        _known.update({value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]})


def reset() -> None:
    _known.clear()


def _read_keychain(item: str) -> str:
    tool = shutil.which("security")
    if not tool:
        raise NotConnected("This machine has no macOS Keychain tool.")
    try:
        done = subprocess.run(  # noqa: S603
            [tool, "find-generic-password", "-s", item, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise NotConnected(f"The Keychain did not answer for item {item}.") from None
    if done.returncode != 0:
        raise NotConnected(f"The Keychain has no readable item {item}.")
    return done.stdout.strip()


def _read_file(path_text: str, json_key: str | None) -> str:
    path = Path(path_text).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise NotConnected(f"The secret file {path} is not readable.") from None
    if json_key is None:
        return text
    try:
        value = json.loads(text)[json_key]
    except (ValueError, KeyError, TypeError):
        raise NotConnected(f"The secret file {path} has no field {json_key}.") from None
    if not isinstance(value, str):
        raise NotConnected(f"The field {json_key} in {path} is not text.")
    return value


def read(source: dict[str, str]) -> str:
    if "env" in source:
        value = os.environ.get(source["env"], "")
        where = f"environment variable {source['env']}"
    elif "file" in source:
        value = _read_file(source["file"], source.get("json_key"))
        where = f"file {source['file']}"
    else:
        value = _read_keychain(source["keychain"])
        where = f"Keychain item {source['keychain']}"
    if not value:
        raise NotConnected(f"The {where} is empty or not set.")
    remember(value)
    return value


def _scrub_text(text: str) -> str:
    for value in sorted(_known, key=len, reverse=True):
        text = text.replace(value, "*")
    for pattern, replacement in SECRET_SHAPES:
        text = pattern.sub(replacement, text)
    return text


def scrub[T](value: T) -> T:
    clean: Any = value
    if isinstance(value, str):
        clean = _scrub_text(value)
    elif isinstance(value, dict):
        clean = {scrub(key): scrub(item) for key, item in value.items()}
    elif isinstance(value, list):
        clean = [scrub(item) for item in value]
    return cast(T, clean)


def safe_reason(text: str) -> str:
    """Scrub before the cut: a cut key block loses the end marker that the scrub needs."""
    return _scrub_text(text)[:REASON_LIMIT]
