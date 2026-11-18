"""Read existing coding-plan credentials without modifying their owners' stores."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path

from .common import ProviderError, object_dict, objects, read_json

_CODEXBAR_IDS = {"kimi-coding": "kimi", "zai-glm": "zai", "opencode-go": "opencodego"}
_OPENCODE_IDS = {"kimi-coding": "kimi-for-coding", "zai-glm": "zai-coding-plan", "opencode-go": "opencode-go"}
_KIMI_HOSTS = ("https://api.kimi.com", "https://api.kimi.ai")


def _key(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _home_path(variable: str, default: Path) -> Path:
    value = Path(os.environ.get(variable, ""))
    return value if value.is_absolute() else default


def _codexbar_key(provider: str, home: Path) -> str:
    config = read_json(home / ".config/codexbar/config.json")
    if config is None:
        config = read_json(home / ".codexbar/config.json") or {}
    for row in objects(config.get("providers")):
        if row.get("id") == _CODEXBAR_IDS[provider] and row.get("enabled") is not False:
            return _key(row.get("apiKey"))
    return ""


def _opencode_key(provider: str, home: Path) -> str:
    directory = _home_path("XDG_DATA_HOME", home / ".local/share") / "opencode"
    filename = os.environ.get("OPENCODE_DB") or "opencode.db"
    entry_id = _OPENCODE_IDS[provider]
    database = directory / filename
    if filename != ":memory:" and database.is_file():
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as db:
            has_table = db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'credential'").fetchone()
            if has_table:
                entry = _active_opencode_entry(db, entry_id)
                if not entry and provider == "opencode-go":
                    entry = _active_opencode_entry(db, "opencode")
                return _opencode_db_key(provider, entry)
    entry = object_dict((read_json(directory / "auth.json") or {}).get(entry_id))
    return _key(entry.get("key")) if entry.get("type") == "api" else ""


def _active_opencode_entry(db: sqlite3.Connection, entry_id: str) -> dict:
    rows = db.execute(
        "SELECT value FROM credential WHERE active = 1 AND integration_id = ? LIMIT 2",
        (entry_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ProviderError("Multiple active OpenCode credentials. Select one in OpenCode or configure a key")
    return object_dict(json.loads(rows[0][0])) if rows else {}


def _opencode_db_key(provider: str, entry: dict) -> str:
    if entry.get("type") == "key":
        return _key(entry.get("key"))
    if provider == "opencode-go" and entry.get("type") == "oauth":
        expiry = entry.get("expires")
        if type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time() * 1000:
            return _key(entry.get("access"))
        raise ProviderError("OpenCode Go login expired. Sign in again in OpenCode")
    return ""


def _kimi_cli_key(home: Path) -> tuple[str, str | None]:
    directory = _home_path("KIMI_CODE_HOME", home / ".kimi-code")
    try:
        with (directory / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return "", None
    providers = object_dict(config.get("providers"))
    data = object_dict(providers.get("managed:kimi-code"))
    base = str(data.get("base_url", "")).rstrip("/")
    for host in _KIMI_HOSTS:
        if base in (f"{host}/coding", f"{host}/coding/v1"):
            return _key(data.get("api_key")), f"{host}/coding/v1/usages"
    return "", None


def local_coding_credential(provider: str, home: Path) -> tuple[str, str, str | None]:
    """Return an existing key, its display label and a fixed regional endpoint."""
    try:
        if key := _codexbar_key(provider, home):
            return key, "CodexBar", None
        if key := _opencode_key(provider, home):
            return key, "OpenCode", None
        if provider == "kimi-coding":
            key, url = _kimi_cli_key(home)
            if key:
                return key, "Kimi CLI", url
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise ProviderError("Cannot read existing coding-plan credentials. Configure a key in providers.toml") from None
    return "", "-", None
