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
import tomllib
from dataclasses import dataclass
from pathlib import Path

from claude_swap.paths import get_backup_root

CONFIG_NAME = "external.toml"
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


def fetch(source: Source) -> list[ExternalAccount]:
    """Run the list command; any failure becomes one error row for the source."""
    def failed(message: str) -> list[ExternalAccount]:
        return [ExternalAccount(source.name, "-", error=message)]

    try:
        proc = _run(list(source.list_cmd), source.timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return failed(f"{type(exc).__name__}: {exc}"[:160])
    if proc.returncode != 0:
        return failed(_last_line(proc))
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return failed("list output is not JSON")
    if not isinstance(rows, list):
        return failed("list output is not a JSON array")
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
        ))
    return accounts or failed("no accounts")


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
