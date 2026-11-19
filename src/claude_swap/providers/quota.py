"""Collect provider quota rows and cache only the rendered data, per provider."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path

from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock

from .agy import AgyAccounts, agy_quota
from .api import CODING_ENDPOINTS, coding_plan_jobs
from .claude import claude_jobs
from .codex import CodexAccounts, codex_quota
from .common import ProviderAccountQuota, ProviderError, QuotaWindow, ResetCredits, home, object_dict, read_json, write_json

PROVIDERS = ("claude", "agy", "codex", *CODING_ENDPOINTS)
SWITCHERS = {"agy": AgyAccounts, "codex": CodexAccounts}
TTL = 60
CLAIM_TTL = 180


def quota_payload(rows: list[ProviderAccountQuota]) -> dict:
    # JSON arrays are intentional even though the immutable Python rows use tuples.
    accounts = [asdict(row) for row in rows]
    for row in accounts:
        row.pop("login_ref")
    return {"schemaVersion": 1, "accounts": accounts}


def _load_cache(path: Path) -> dict:
    try:
        return read_json(path) or {}
    except (OSError, ValueError):
        return {}


def _decode_rows(entry: dict) -> list[ProviderAccountQuota] | None:
    try:
        rows = []
        for raw in entry["rows"]:
            item = dict(raw)
            item["windows"] = tuple(QuotaWindow(**win) for win in item["windows"])
            if item.get("reset_credits") is not None:
                item["reset_credits"] = ResetCredits(**item["reset_credits"])
            rows.append(ProviderAccountQuota(**item))
        return rows
    except (KeyError, ValueError, TypeError):
        return None


def _deadline(entry: dict, name: str) -> float:
    value = entry.get(name, 0)
    return value if type(value) in (int, float) and math.isfinite(value) else 0


def _matches_roster(rows: list[ProviderAccountQuota], codex_accounts) -> bool:
    if codex_accounts is None:
        return True
    expected = {(a.label, a.login.identity, a.active) for a in codex_accounts[0]}
    return {(r.account, r.login_ref, r.active) for r in rows if r.account != "-"} == expected


def _claim(path: Path, names: tuple[str, ...], started: float, max_age: float,
           refresh: bool, refresh_login: bool, codex_accounts) -> tuple[dict, list[str]]:
    with FileLock(path.with_suffix(".lock"), timeout=2):
        data = _load_cache(path)
        rows, pending = {}, []
        for name in names:
            entry = object_dict(data.get(name))
            view = _decode_rows(entry)
            in_progress = _deadline(entry, "claim_until") > started
            waiting = _deadline(entry, "retry_at") > started
            fresh = _fresh(entry, name, view, started, max_age, refresh_login, codex_accounts)
            if in_progress or waiting or (view is not None and fresh and not refresh):
                if view is not None and not fresh:
                    view = [replace(row, status="stale") if row.status == "ok" else row for row in view]
                rows[name] = view if view is not None else [ProviderAccountQuota(
                    name, "-", status="pending" if in_progress else "backoff",
                    error="Quota refresh already in progress" if in_progress else "Provider asked to retry later",
                    retry_at=entry.get("retry_at"),
                )]
                continue
            data[name] = {**entry, "started_at": started, "claim_started_at": started,
                          "claim_until": time.time() + CLAIM_TTL}
            pending.append(name)
        if pending:
            write_json(path, data)
        return rows, pending


def _fresh(entry: dict, name: str, rows: list[ProviderAccountQuota] | None, started: float,
           max_age: float, refresh_login: bool, codex_accounts) -> bool:
    if rows is None or not 0 <= started - _deadline(entry, "fetched_at") < max_age:
        return False
    if name == "codex":
        return (_matches_roster(rows, codex_accounts)
                and not (refresh_login and entry.get("probe_only")))
    return True


def _update_cache(path: Path, changes: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(path.with_suffix(".lock"), timeout=0.5):
            data = _load_cache(path)
            for name, entry in changes.items():
                current = object_dict(data.get(name))
                if _deadline(current, "started_at") <= entry["started_at"]:
                    data[name] = entry
                elif current.get("claim_started_at") == entry["started_at"]:
                    # The accounts changed while the read ran. Drop the old rows,
                    # but keep a server deadline and release the original claim.
                    current.pop("claim_started_at", None)
                    current.pop("claim_until", None)
                    current["retry_at"] = max(_deadline(current, "retry_at"), entry["retry_at"])
            write_json(path, data)
    except (LockError, OSError, ValueError, TypeError):
        pass  # The measurement is still available when the cache cannot be written.


def invalidate_quota(provider: str, root: Path | None = None) -> None:
    root = home() if root is None else root
    path = root / "quota_cache.json"
    try:
        with FileLock(path.with_suffix(".lock"), timeout=2):
            data = _load_cache(path)
            previous = object_dict(data.get(provider))
            now = time.time()
            entry = {"started_at": now, "retry_at": previous.get("retry_at")}
            if _deadline(previous, "claim_until") > now:
                entry["claim_until"] = previous["claim_until"]
                entry["claim_started_at"] = previous.get("claim_started_at", previous.get("started_at"))
            data[provider] = entry
            write_json(path, data)
    except (OSError, LockError):
        pass


def _jobs(provider: str, root: Path, switcher=None, *,
          refresh_login: bool = True, codex_accounts=None,
          refresh: bool = False) -> list[Callable[[], ProviderAccountQuota]]:
    if provider == "claude":
        if switcher is None:
            from claude_swap.switcher import ClaudeAccountSwitcher

            switcher = ClaudeAccountSwitcher()
        return claude_jobs(switcher, refresh=refresh)
    if provider in SWITCHERS:
        store = SWITCHERS[provider](root)
        accounts, error = codex_accounts if provider == "codex" and codex_accounts is not None else store.roster()
        jobs = [
            (lambda account=account: agy_quota(store, account)) if provider == "agy"
            else (lambda account=account: codex_quota(account, store, refresh_login=refresh_login))
            for account in accounts
        ]
        if error:
            jobs.append(lambda: ProviderAccountQuota(provider, "-", status="error", error=error))
        return jobs
    return coding_plan_jobs(root, provider)


def _run_job(provider: str, job: Callable[[], ProviderAccountQuota]) -> ProviderAccountQuota:
    try:
        row = job()
        return replace(row, fetched_at=time.time()) if row.status == "ok" and row.fetched_at is None else row
    except ProviderError as exc:
        return ProviderAccountQuota(provider, "-", status="error", error=str(exc),
                                    retry_at=exc.retry_after, http_status=exc.status_code)
    except Exception:
        return ProviderAccountQuota(provider, "-", status="error", error="Quota lookup failed")


def collect_quota(providers: Iterable[str] = PROVIDERS, *, refresh: bool = False,
                  root: Path | None = None, switcher=None, max_age: float = TTL,
                  refresh_login: bool = True, codex_accounts=None) -> list[ProviderAccountQuota]:
    root = home() if root is None else root
    path = root / "quota_cache.json"
    names = tuple(dict.fromkeys(providers))
    started = time.time()
    # Fresh cache hits take no lock and do not touch credential stores.
    cache = _load_cache(path)
    rows = {}
    pending = []
    for name in names:
        entry = object_dict(cache.get(name))
        cached = _decode_rows(entry) if not refresh else None
        if not _fresh(entry, name, cached, started, max_age, refresh_login, codex_accounts):
            cached = None
        if cached is None:
            pending.append(name)
        else:
            rows[name] = cached
    if pending:
        try:
            existing, pending = _claim(path, tuple(pending), started, max_age, refresh, refresh_login, codex_accounts)
            rows.update(existing)
        except (OSError, LockError, ValueError):
            for name in pending:
                rows[name] = [ProviderAccountQuota(name, "-", status="error", error="Quota cache unavailable")]
            pending = []
    if pending:
        with (
            ThreadPoolExecutor(max_workers=len(pending)) as discovery,
            ThreadPoolExecutor(max_workers=8) as requests,
            ThreadPoolExecutor(max_workers=2) as agy_requests,
        ):
            options = {} if refresh_login and codex_accounts is None else {
                "refresh_login": refresh_login, "codex_accounts": codex_accounts,
            }
            discovered = {
                discovery.submit(_jobs, name, root, switcher, **options,
                                 **({"refresh": refresh} if name == "claude" else {})): name
                for name in pending
            }
            # Submit each provider as soon as its roster is ready, even when another
            # provider is waiting for a Keychain prompt.
            tasks = {}
            for future in as_completed(discovered):
                name = discovered[future]
                try:
                    pool = agy_requests if name == "agy" else requests
                    tasks[name] = [pool.submit(_run_job, name, job) for job in future.result()]
                except ProviderError as exc:
                    rows[name] = [ProviderAccountQuota(name, "-", status="error", error=str(exc),
                                                       retry_at=exc.retry_after, http_status=exc.status_code)]
                except Exception:
                    rows[name] = [ProviderAccountQuota(name, "-", status="error", error="Cannot read provider accounts")]
            for name, futures in tasks.items():
                rows[name] = [future.result() for future in futures]
        changes = {
            name: {"started_at": started, "fetched_at": time.time(),
                   "retry_at": max((row.retry_at or 0 for row in rows[name]), default=0),
                   "probe_only": not refresh_login and name == "codex" and
                   any(row.http_status == 401 for row in rows[name]),
                   "rows": [asdict(row) for row in rows[name]]}
            for name in pending
        }
        _update_cache(path, changes)
    return [_with_claude_inventory(row) for name in names for row in rows[name] or ()]


def _with_claude_inventory(row: ProviderAccountQuota) -> ProviderAccountQuota:
    """Claude rows show the reset credits the CLI identity read (claude_resets),
    cached rows included: cswap's own usage read only ever sees 'not offered'."""
    if row.provider != "claude":
        return row
    from .claude_resets import cached

    email = row.account.rsplit("(", 1)[-1].rstrip(")") if row.account.endswith(")") else row.account
    credits = cached(email)
    return replace(row, reset_credits=credits) if credits is not None else row
