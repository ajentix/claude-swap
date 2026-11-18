"""External account sources shown on the watch screen, below the Claude accounts.

A source is a pair of commands configured in ``<backup root>/external.toml``
(``~/.claude-swap-backup/external.toml`` on Windows/macOS)::

    [[source]]
    name = "agy"
    list = ["py", "-X", "utf8", "C:/tools/agy_quota.py"]
    switch = ["py", "-X", "utf8", "C:/tools/agy_switch.py", "{account}"]  # optional
    every = 300     # seconds between list runs (default 120)
    timeout = 240   # seconds before a run is abandoned (default 120)

``list`` prints a JSON array, one object per account::

    {"account": "me@example.com", "active": true, "plan": "pro", "note": "",
     "error": "", "windows": [{"name": "5h", "used_pct": 18.0, "resets": "17:49", "note": ""}]}

``used_pct`` is null for a window with no figure (its ``note`` says why).
Secrets stay with the commands: cswap only sees the usage they print.
Without ``switch`` the source is view-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from claude_swap.fsutil import atomic_write_text, read_text_with_retry
from claude_swap.paths import get_backup_root

CONFIG_NAME = "external.toml"
STORE_NAME = "external_store.json"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True)
class ExternalWindow:
    name: str
    used_pct: float | None
    resets: str | None = None
    note: str = ""


@dataclass(frozen=True)
class ExternalAccount:
    source: str
    account: str
    active: bool = False
    plan: str | None = None
    note: str = ""
    error: str = ""
    windows: tuple[ExternalWindow, ...] = ()
    switchable: bool = False
    fetched_at: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.account


@dataclass(frozen=True)
class Source:
    name: str
    list_cmd: tuple[str, ...]
    switch_cmd: tuple[str, ...] = ()
    every: float = 120.0
    timeout: float = 120.0


def config_path() -> Path:
    return get_backup_root() / CONFIG_NAME


def load_sources(path: Path | None = None) -> list[Source]:
    """Configured sources; an absent or unreadable file means none."""
    try:
        with (path or config_path()).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, ValueError):
        return []
    sources = []
    for entry in raw.get("source", []):
        if not isinstance(entry, dict):
            continue
        name, cmd = entry.get("name"), entry.get("list")
        if not isinstance(name, str) or not _is_cmd(cmd):
            continue
        switch = entry.get("switch")
        sources.append(Source(
            name=name,
            list_cmd=tuple(cmd),
            switch_cmd=tuple(switch) if _is_cmd(switch) else (),
            every=float(entry.get("every", 120)),
            timeout=float(entry.get("timeout", 120)),
        ))
    return sources


def _is_cmd(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) for x in value)


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=timeout, creationflags=_NO_WINDOW,
    )


def _last_line(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1][:160] if lines else f"exit {proc.returncode}"


def _parse(source: Source, rows: list, fetched_at: float | None) -> list[ExternalAccount]:
    accounts = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("account"), str):
            continue
        windows = tuple(
            ExternalWindow(
                name=str(w.get("name", "")),
                used_pct=float(w["used_pct"]) if isinstance(w.get("used_pct"), (int, float)) else None,
                resets=str(w["resets"]) if w.get("resets") else None,
                note=str(w.get("note") or ""),
            )
            for w in row.get("windows") or [] if isinstance(w, dict)
        )
        accounts.append(ExternalAccount(
            source=source.name,
            account=row["account"],
            active=bool(row.get("active")),
            plan=str(row["plan"]) if row.get("plan") else None,
            note=str(row.get("note") or ""),
            error=str(row.get("error") or ""),
            windows=windows,
            switchable=bool(source.switch_cmd) and row["account"] != "-",
            fetched_at=fetched_at,
        ))
    return accounts


# -- the store: last good rows per source, shared by every cswap process ----------
# Like the Claude usage store: screens paint from it at once, a run happens only
# when the entry is older than the source's interval, an in-flight claim keeps two
# processes from running the same command, and a failed run keeps the last rows.

_store_lock = threading.Lock()


def store_path() -> Path:
    return get_backup_root() / STORE_NAME


def _read_store() -> dict:
    try:
        data = json.loads(read_text_with_retry(store_path()))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_store(name: str, **fields) -> None:
    with _store_lock:
        data = _read_store()
        entry = data.get(name) if isinstance(data.get(name), dict) else {}
        entry.update(fields)
        data[name] = entry
        atomic_write_text(store_path(), json.dumps(data, ensure_ascii=False))


def stored(source: Source) -> list[ExternalAccount]:
    """The last good rows, without running anything (empty when never fetched)."""
    entry = _read_store().get(source.name) or {}
    rows = entry.get("rows")
    return _parse(source, rows, entry.get("at")) if isinstance(rows, list) else []


def due(source: Source, now: float | None = None) -> bool:
    entry = _read_store().get(source.name) or {}
    now = time.time() if now is None else now
    fresh = now - float(entry.get("at") or 0) < source.every
    claimed = now - float(entry.get("claimed_at") or 0) < source.timeout
    return not fresh and not claimed


def fetch(source: Source, *, force: bool = False) -> list[ExternalAccount]:
    """Rows for the source: the store's while fresh or claimed, else a new run.

    A failed run keeps the last good rows (with the error on the first) or, with
    none stored, becomes one error row.
    """
    if not force and not due(source):
        return stored(source)
    _update_store(source.name, claimed_at=time.time())
    error = ""
    try:
        proc = _run(list(source.list_cmd), source.timeout)
        if proc.returncode != 0:
            error = _last_line(proc)
        else:
            rows = json.loads(proc.stdout)
            if not isinstance(rows, list) or not _parse(source, rows, None):
                error = "list output has no accounts"
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:160]
    except ValueError:
        error = "list output is not JSON"
    if not error:
        now = time.time()
        _update_store(source.name, at=now, rows=rows, claimed_at=0)
        return _parse(source, rows, now)
    _update_store(source.name, claimed_at=0)
    last = stored(source)
    if not last:
        return [ExternalAccount(source.name, "-", error=error)]
    return [replace(last[0], error=error), *last[1:]]


def mark_active(source: Source, account: str) -> None:
    """After a switch, move the stored active flag so every screen shows it now."""
    with _store_lock:
        data = _read_store()
        entry = data.get(source.name)
        if not isinstance(entry, dict) or not isinstance(entry.get("rows"), list):
            return
        for row in entry["rows"]:
            if isinstance(row, dict):
                row["active"] = row.get("account") == account
        atomic_write_text(store_path(), json.dumps(data, ensure_ascii=False))


def switch(source: Source, account: str) -> tuple[bool, str]:
    """Run the switch command for one account: (ok, last output line)."""
    if not source.switch_cmd:
        return False, f"{source.name} is view-only"
    cmd = [part.replace("{account}", account) for part in source.switch_cmd]
    try:
        proc = _run(cmd, source.timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0:
        return False, _last_line(proc)
    return True, lines[-1][:160] if lines else f"switched {source.name} to {account}"
