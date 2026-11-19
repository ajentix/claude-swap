"""Claude reset credits, read the way the Claude Code CLI reads them.

The usage endpoint answers ``cedar_ember: {eligible: false, ineligible_reason:
"surface"}`` to a client it does not recognise, so cswap's own User-Agent sees
"not offered" even on a login holding resets. The CLI identity CodexBar also
sends, ``claude-cli/<version> (external, cli)``, gets the real inventory.

Ordinary usage polling keeps cswap's own identity: the endpoint budgets
requests from non-first-party agents and cswap paces itself for that budget.
Only this rare inventory read, at most every ``TTL_S`` per account, and the
reset command (which reads the inventory and redeems from it) use the CLI
identity. The last inventory per account is kept in ``claude_reset_credits.json``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, fields
from functools import lru_cache
from pathlib import Path

from claude_swap.exceptions import LockError
from claude_swap.fsutil import atomic_write_text, read_text_with_retry
from claude_swap.locking import FileLock

from .common import ProviderError, ResetCredits, home, process_options

TTL_S = 6 * 3600
RETRY_S = 1800  # after a failed read, before trying that account again
CACHE_NAME = "claude_reset_credits.json"
FALLBACK_VERSION = "2.1.0"


@lru_cache(maxsize=1)
def cli_version() -> str:
    """The installed Claude Code version, or a recent one when it cannot be found."""
    exe = shutil.which("claude")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL, **process_options()).stdout
            match = re.search(r"\d+\.\d+\.\d+", out or "")
            if match:
                return match.group(0)
        except (OSError, subprocess.SubprocessError):
            pass
    return FALLBACK_VERSION


def user_agent() -> str:
    return f"claude-cli/{cli_version()} (external, cli)"


def _path(root: Path | None) -> Path:
    return (home() if root is None else root) / CACHE_NAME


def _load(root: Path | None) -> dict:
    try:
        data = json.loads(read_text_with_retry(_path(root)))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _credits(value: object) -> ResetCredits | None:
    if not isinstance(value, dict):
        return None
    known = {f.name for f in fields(ResetCredits)}
    try:
        return ResetCredits(**{k: v for k, v in value.items() if k in known})
    except TypeError:
        return None


def cached(email: str, root: Path | None = None) -> ResetCredits | None:
    """The last inventory read for this account, however old; None if never read."""
    entry = _load(root).get(email)
    return _credits(entry.get("credits")) if isinstance(entry, dict) else None


def due(email: str, root: Path | None = None, now: float | None = None) -> bool:
    entry = _load(root).get(email)
    if not isinstance(entry, dict):
        return True
    now = time.time() if now is None else now
    read_at = entry.get("at") if type(entry.get("at")) in (int, float) else 0
    tried_at = entry.get("attempted_at") if type(entry.get("attempted_at")) in (int, float) else 0
    return now - read_at >= TTL_S and now - tried_at >= RETRY_S


def _save(email: str, root: Path | None, **fields_) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(path.with_suffix(".lock")):
        data = _load(root)
        entry = data.get(email) if isinstance(data.get(email), dict) else {}
        entry.update(fields_)
        data[email] = entry
        atomic_write_text(path, json.dumps(data, ensure_ascii=False))


def refresh(email: str, credentials: str, root: Path | None = None) -> ResetCredits | None:
    """Read one account's inventory with the CLI identity and keep it."""
    from .api import get_usage
    from .claude import claude_headers
    from .reset_credits import CLAUDE_USAGE_URL, claude_reset_offers

    now = time.time()
    try:
        raw = get_usage(CLAUDE_USAGE_URL, claude_headers(credentials, cli_identity=True))
    except ProviderError:
        _save(email, root, attempted_at=now)
        raise
    credits, _ = claude_reset_offers(raw)
    _save(email, root, at=now, attempted_at=now, credits=None if credits is None else asdict(credits))
    return credits


def refresh_due(accounts, root: Path | None = None) -> int:
    """Refresh every account whose inventory is older than ``TTL_S``.

    ``accounts`` yields ``(email, credentials)``. Returns how many were read.
    Failures are recorded and retried after ``RETRY_S``.
    """
    read = 0
    for email, credentials in accounts:
        if not email or not due(email, root):
            continue
        try:
            refresh(email, credentials, root)
            read += 1
        except (ProviderError, LockError, OSError, ValueError):
            continue
    return read
