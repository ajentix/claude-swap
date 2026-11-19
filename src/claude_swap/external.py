"""Other accounts on the watch screen, drawn like the Claude accounts.

Built in, with no configuration: Antigravity and Codex logins (the ones
``cswap agy``/``cswap codex`` manage, see ``providers/``) and the Kimi Coding,
z.ai and OpenCode Go coding plans whose keys ``providers/api.py`` can find.
Providers with nothing configured stay hidden.

``<backup root>/external.toml`` (``~/.claude-swap-backup/external.toml`` on
Windows/macOS) can replace a built-in source by name, turn one off, or add a
source backed by your own commands::

    [[source]]
    name = "codex"
    enabled = false

    [[source]]
    name = "plans"                 # replaces the built-in coding plans
    title = "Coding plans"
    list = ["python", "/path/to/plan_usage.py"]
    switch = ["python", "/path/to/switch.py", "{account}"]   # optional
    every = 120     # seconds between runs (default 120)
    timeout = 120   # seconds before a run is abandoned (default 120)

``list`` prints a JSON array, one object per account::

    {"account": "me@example.com", "product": "Kimi", "active": true, "plan": "pro", "note": "", "error": "",
     "windows": [{"name": "Kimi 5h", "used_pct": 18.0, "resets_at": "2026-11-19T08:13:00Z", "note": ""}]}

``used_pct`` is the share used (null: no figure, ``note`` says why);
``resets_at`` is an ISO time or epoch seconds, drawn like a Claude reset.
Secrets stay with the commands: cswap only sees the usage they print.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.fsutil import atomic_write_text, read_text_with_retry
from claude_swap.paths import get_backup_root

CONFIG_NAME = "external.toml"
STORE_NAME = "external_store.json"
PLANS_NAME = "external_plans.json"  # plan names you set by hand: cswap plan
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True)
class ExternalWindow:
    name: str
    used_pct: float | None
    resets_at: str | None = None  # ISO UTC; drawn as "resets 2h 13m · 20:39"
    note: str = ""
    resets: str | None = None  # a preformatted reset text, when no resets_at was given


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
    product: str = ""  # header name ("Codex", "GLM"); default: the source label

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.account


@dataclass(frozen=True)
class Source:
    name: str
    list_cmd: tuple[str, ...] = ()
    title: str = ""
    label: str = ""
    merge: bool = False
    switch_cmd: tuple[str, ...] = ()
    every: float = 120.0
    timeout: float = 120.0
    providers: tuple[str, ...] = ()  # built-in providers/ instead of commands


# Built-in sources, in display order, each in its own section: a product
# switches on its own, so its accounts stay together (merge = true in
# external.toml draws a same-email login on the Claude card instead).
BUILTIN = (
    Source("agy", title="Antigravity", label="agy", every=300, timeout=240, providers=("agy",)),
    Source("codex", title="Codex", label="Codex", providers=("codex",)),
    Source("plans", title="Coding plans", providers=("kimi-coding", "zai-glm", "opencode-go")),
)
# Window prefix per built-in provider ("Kimi 5h"), beside the source label.
PRODUCT = {"kimi-coding": "Kimi", "zai-glm": "GLM", "opencode-go": "OpenCode"}


def config_path() -> Path:
    return get_backup_root() / CONFIG_NAME


def load_sources(path: Path | None = None) -> list[Source]:
    """The built-in sources, as ``external.toml`` replaces, disables or adds to them."""
    try:
        with (path or config_path()).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, ValueError):
        raw = {}
    sources = {s.name: s for s in BUILTIN}
    for entry in raw.get("source", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if entry.get("enabled") is False:
            sources.pop(name, None)
            continue
        base = sources.get(name) or Source(name)
        cmd, switch = entry.get("list"), entry.get("switch")
        if _is_cmd(cmd):
            base = replace(base, list_cmd=tuple(cmd), providers=(),
                           switch_cmd=tuple(switch) if _is_cmd(switch) else ())
        elif not base.providers:
            continue  # a new source needs a list command
        sources[name] = replace(
            base,
            title=str(entry.get("title") or base.title or name),
            label=str(entry.get("label", base.label) or ""),
            merge=bool(entry.get("merge", base.merge)),
            every=float(entry.get("every", base.every)),
            timeout=float(entry.get("timeout", base.timeout)),
        )
    return list(sources.values())


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
    by_hand = plan_overrides().get(source.name, {})
    accounts = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("account"), str):
            continue
        windows = tuple(
            ExternalWindow(
                name=str(w.get("name", "")),
                used_pct=float(w["used_pct"]) if isinstance(w.get("used_pct"), (int, float)) else None,
                resets_at=_iso(w.get("resets_at")),
                note=str(w.get("note") or ""),
                resets=str(w["resets"]) if w.get("resets") else None,
            )
            for w in row.get("windows") or [] if isinstance(w, dict)
        )
        accounts.append(ExternalAccount(
            source=source.name,
            account=row["account"],
            active=bool(row.get("active")),
            plan=by_hand.get(row["account"]) or by_hand.get("*") or (str(row["plan"]) if row.get("plan") else None),
            note=str(row.get("note") or ""),
            error=str(row.get("error") or ""),
            windows=windows,
            switchable=(bool(source.switch_cmd) or bool(row.get("switchable"))) and row["account"] != "-",
            product=str(row.get("product") or ""),
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
        if source.providers:
            rows = _builtin_rows(source, force)  # empty: nothing configured, the source hides
        else:
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
    except Exception as exc:  # a built-in provider failing must not take the screen down
        error = f"{source.name} lookup failed: {type(exc).__name__}"
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
    """Switch one account: the provider's login store, or the switch command."""
    if source.providers:
        return _builtin_switch(source, account)
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


# -- built-in providers --------------------------------------------------------------

def _iso(value: object) -> str | None:
    """ISO UTC from an ISO string or epoch seconds/milliseconds; None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value / 1000 if value >= 1e11 else value, timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _window_order(w) -> tuple:
    """Claude rows read 5h, 7d, then per-model; external windows follow the same
    order: Claude family first, then by window length."""
    family, _, period = w.name.replace("Claude and GPT", "Claude").rpartition(" ")
    unit = {"m": 60, "h": 3600, "d": 86400}.get(period[-1:], 0)
    try:
        seconds = float(period[:-1]) * unit if unit else float("inf")
    except ValueError:
        seconds = float("inf")
    return (family != "Claude", family, seconds, w.name)


def _builtin_rows(source: Source, force: bool) -> list[dict]:
    from claude_swap.providers.quota import collect_quota

    rows = []
    for row in collect_quota(source.providers, refresh=force):
        if row.status == "unconfigured":
            continue
        account = row.account
        if row.provider in PRODUCT:  # coding plans: name the plan, not the key's label
            account = row.provider if account in ("-", "default", "CodexBar", "OpenCode", "Kimi CLI") \
                else f"{row.provider} {account}"
        product = PRODUCT.get(row.provider, "")
        note = ""
        credits = row.reset_credits
        if credits and credits.available_count:
            note = f"{credits.available_count} reset credit{'s' if credits.available_count != 1 else ''}"
        rows.append({
            "account": account,
            "product": product or source.label,
            "active": row.active,
            "plan": row.plan,
            "note": note,
            "error": row.error or "",
            "switchable": bool(row.switchable),
            "windows": [{
                "name": " ".join(x for x in (product, w.name.replace("Claude and GPT", "Claude")) if x),
                "used_pct": None if w.disabled or w.remaining_pct is None else round(100 - w.remaining_pct, 1),
                "resets_at": w.resets_at,
                "note": "blocked" if w.disabled else "",
            } for w in sorted(row.windows, key=_window_order)],
        })
    return rows


def _builtin_switch(source: Source, account: str) -> tuple[bool, str]:
    from claude_swap.exceptions import LockError
    from claude_swap.providers.common import ProviderError, home
    from claude_swap.providers.quota import SWITCHERS, invalidate_quota

    switchers = [p for p in source.providers if p in SWITCHERS]
    if not switchers:
        return False, f"{source.name} is view-only"
    provider = switchers[0]
    try:
        label = SWITCHERS[provider](home()).switch(account)
    except ProviderError as exc:
        return False, str(exc)
    except (LockError, OSError, ValueError):
        return False, "Could not access the local login store"
    invalidate_quota(provider)
    return True, f"Active {provider} account {label}"


# -- plan names set by hand -------------------------------------------------------------
# For what a provider does not report (an Antigravity tier, a Kimi membership):
# ``cswap plan <source> <account|*> <name>``. A name set here wins over the provider's.

def _plans_path() -> Path:
    return get_backup_root() / PLANS_NAME


def plan_overrides() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(read_text_with_retry(_plans_path()))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {s: {a: n for a, n in accounts.items() if isinstance(n, str) and n.strip()}
            for s, accounts in data.items() if isinstance(accounts, dict)}


def set_plan(source: str, account: str, name: str | None) -> None:
    with _store_lock:
        data = plan_overrides()
        accounts = data.setdefault(source, {})
        if name and name.strip():
            accounts[account] = name.strip()
        else:
            accounts.pop(account, None)
        data = {s: a for s, a in data.items() if a}
        atomic_write_text(_plans_path(), json.dumps(data, ensure_ascii=False, indent=1))


def plan_command(argv: list[str], prog: str) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog=f"{prog} plan",
        description="Set the plan name shown for an account on the watch screen, "
                    "for what a provider does not report (an Antigravity tier, a Kimi membership).")
    parser.add_argument("source", nargs="?", help="watch source: agy, codex, plans, or one of yours")
    parser.add_argument("account", nargs="?", help="account as the watch screen shows it, or * for every account")
    parser.add_argument("name", nargs="?", help='plan name, e.g. "Ultra"')
    parser.add_argument("--unset", action="store_true", help="remove the name")
    args = parser.parse_args(argv)
    if args.source is None:
        plans = plan_overrides()
        if not plans:
            print("No plan names set. Example: cswap plan agy you@example.com Ultra")
        for source, accounts in plans.items():
            for account, name in accounts.items():
                print(f"{source}  {account}  {name}")
        return 0
    if args.account is None or (args.name is None and not args.unset):
        parser.error("give a source, an account (or *) and a name, or --unset")
    known = {s.name for s in load_sources()}
    if args.source not in known:
        parser.error(f"unknown source {args.source!r}; sources: {', '.join(sorted(known))}")
    set_plan(args.source, args.account, None if args.unset else args.name)
    shown = "removed" if args.unset else f"set to {args.name.strip()}"
    print(f"{args.source} {args.account}: plan {shown}")
    return 0
