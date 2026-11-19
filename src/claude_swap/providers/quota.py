"""Collect provider quota rows and cache only the rendered data, per provider."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
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


def quota_payload(rows: list[ProviderAccountQuota]) -> dict:
    # JSON arrays are intentional even though the immutable Python rows use tuples.
    return {"schemaVersion": 1, "accounts": [asdict(row) for row in rows]}


def _load_cache(path: Path) -> dict:
    try:
        return read_json(path) or {}
    except (OSError, ValueError):
        return {}


def _cached_rows(entry: dict, now: float) -> list[ProviderAccountQuota] | None:
    try:
        if not 0 <= now - entry["fetched_at"] < TTL:
            return None
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


def _update_cache(path: Path, changes: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(path.with_suffix(".lock"), timeout=0.5):
            data = _load_cache(path)
            for name, entry in changes.items():
                if object_dict(data.get(name)).get("started_at", 0) <= entry["started_at"]:
                    data[name] = entry
            write_json(path, data)
    except (LockError, OSError, ValueError, TypeError):
        pass  # The measurement is still available when the cache cannot be written.


def invalidate_quota(provider: str, root: Path | None = None) -> None:
    root = home() if root is None else root
    _update_cache(root / "quota_cache.json", {provider: {"started_at": time.time()}})


def _jobs(provider: str, root: Path, switcher=None) -> list[Callable[[], ProviderAccountQuota]]:
    if provider == "claude":
        if switcher is None:
            from claude_swap.switcher import ClaudeAccountSwitcher

            switcher = ClaudeAccountSwitcher()
        return claude_jobs(switcher)
    if provider in SWITCHERS:
        store = SWITCHERS[provider](root)
        accounts, error = store.roster()
        jobs = [
            (lambda account=account: agy_quota(store, account)) if provider == "agy"
            else (lambda account=account: codex_quota(account, store)) for account in accounts
        ]
        if error:
            jobs.append(lambda: ProviderAccountQuota(provider, "-", status="error", error=error))
        return jobs
    return coding_plan_jobs(root, provider)


def _run_job(provider: str, job: Callable[[], ProviderAccountQuota]) -> ProviderAccountQuota:
    try:
        return job()
    except ProviderError as exc:
        return ProviderAccountQuota(provider, "-", status="error", error=str(exc))
    except Exception:
        return ProviderAccountQuota(provider, "-", status="error", error="Quota lookup failed")


def collect_quota(providers: Iterable[str] = PROVIDERS, *, refresh: bool = False,
                  root: Path | None = None, switcher=None) -> list[ProviderAccountQuota]:
    root = home() if root is None else root
    path = root / "quota_cache.json"
    names = tuple(dict.fromkeys(providers))
    started = time.time()
    cache = {} if refresh else _load_cache(path)
    rows = {name: _cached_rows(object_dict(cache.get(name)), started) for name in names}
    pending = [name for name in names if rows[name] is None]
    if pending:
        with (
            ThreadPoolExecutor(max_workers=len(pending)) as discovery,
            ThreadPoolExecutor(max_workers=8) as requests,
            ThreadPoolExecutor(max_workers=2) as agy_requests,
        ):
            discovered = {discovery.submit(_jobs, name, root, switcher): name for name in pending}
            # Submit each provider as soon as its roster is ready, even when another
            # provider is waiting for a Keychain prompt.
            tasks = {}
            for future in as_completed(discovered):
                name = discovered[future]
                try:
                    pool = agy_requests if name == "agy" else requests
                    tasks[name] = [pool.submit(_run_job, name, job) for job in future.result()]
                except ProviderError as exc:
                    rows[name] = [ProviderAccountQuota(name, "-", status="error", error=str(exc))]
                except Exception:
                    rows[name] = [ProviderAccountQuota(name, "-", status="error", error="Cannot read provider accounts")]
            for name, futures in tasks.items():
                rows[name] = [future.result() for future in futures]
        _update_cache(path, {
            name: {"started_at": started, "fetched_at": time.time(), "rows": quota_payload(rows[name])["accounts"]}
            for name in pending
        })
    return [row for name in names for row in rows[name] or ()]
