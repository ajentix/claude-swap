"""Automatic Codex account switching: the rules of ``cswap auto``, for Codex logins.

The active Codex login (``~/.codex/auth.json``) leaves when a window reaches its
line: the 5-hour window against ``threshold``, the weekly and longer windows
against ``weekly_threshold`` (default: the same). It lands on the saved login
(``cswap codex add``) with the most room, as ``cswap auto``'s ``best`` strategy
does:

- a proactive move only lands on a login below every line *and* with at least
  ``margin`` points more room than the active one, so two logins near the line
  never ping-pong;
- when every login is over its lines it moves to the one with the most room
  left, once that is twice the active one's room or 3 points more, whichever
  is less;
- a cooldown (default 5 minutes, shared by every process through
  ``auto_state.json``) follows each switch.

A running Codex session keeps its login; new sessions start on the new one.
The Codex CLI writes refreshed tokens back only to the login it started with,
so a session running across a switch does not undo it.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap.fsutil import atomic_write_text, read_text_with_retry
from claude_swap.locking import FileLock

from .common import ProviderAccountQuota, QuotaWindow, home

COOLDOWN_S = 300.0
MARGIN_PCT = 10.0
STATE_NAME = ".auto-state.json"
LEGACY_STATE_NAME = "auto_state.json"

# Exit codes, as cswap auto --once.
SWITCHED, ERROR, NOTHING, BLOCKED = 0, 1, 2, 3


@dataclass(frozen=True)
class Decision:
    kind: str  # "switch", "no-switch", "blocked", "error"
    reason: str
    source: str | None = None
    target: str | None = None

    @property
    def exit_code(self) -> int:
        return {"switch": SWITCHED, "no-switch": NOTHING, "blocked": BLOCKED}.get(self.kind, ERROR)


def _weekly(window: QuotaWindow) -> bool:
    period = window.name.rsplit(" ", 1)[-1]
    return period.endswith("d")


def _line(window: QuotaWindow, threshold: float, weekly_threshold: float) -> float:
    return weekly_threshold if _weekly(window) else threshold


def _ordinary_windows(row: ProviderAccountQuota) -> list[QuotaWindow]:
    return [w for w in row.windows if re.fullmatch(r"[0-9]+[mhd]", w.name)
            or w.name in {"credits", "Codex"}]


def room(row: ProviderAccountQuota, threshold: float, weekly_threshold: float) -> float | None:
    """Points left before the tightest line (negative: over it); None without readings."""
    windows = _ordinary_windows(row)
    rooms = [_line(w, threshold, weekly_threshold) - (100 - w.remaining_pct)
             for w in windows if w.remaining_pct is not None and not w.disabled]
    if any(w.disabled for w in windows):
        rooms.append(-100.0)  # a blocked window is a window at its limit
    return min(rooms) if rooms else None


def left(row: ProviderAccountQuota) -> float | None:
    """Quota left in the fullest window (0 when one is blocked)."""
    values = [0.0 if w.disabled else w.remaining_pct for w in _ordinary_windows(row)
              if w.disabled or w.remaining_pct is not None]
    return min(values) if values else None


def decide(rows: list[ProviderAccountQuota], threshold: float = 90, weekly_threshold: float | None = None,
           *, margin: float = MARGIN_PCT) -> Decision:
    weekly_threshold = threshold if weekly_threshold is None else weekly_threshold
    if not all(math.isfinite(line) and 0 <= line <= 100 for line in (threshold, weekly_threshold)):
        return Decision("error", "Thresholds must be between 0 and 100 percent")
    active = next((r for r in rows if r.active), None)
    if active is None:
        return Decision("error", "no active Codex login")
    active_room = room(active, threshold, weekly_threshold)
    if active.status != "ok" or active_room is None:
        return Decision("no-switch", f"{active.account}: usage unknown ({active.error or active.status})", active.account)
    if active_room > 0:
        return Decision("no-switch", f"{active.account} under its lines ({active_room:.0f} points left)", active.account)
    candidates = [r for r in rows if not r.active and r.switchable and r.status == "ok"
                  and room(r, threshold, weekly_threshold) is not None]
    if not candidates:
        return Decision("blocked", f"{active.account} reached its line and no other saved login has a reading",
                        active.account)
    under = [r for r in candidates if room(r, threshold, weekly_threshold) > 0
             and room(r, threshold, weekly_threshold) - active_room >= margin]
    if under:
        best = max(under, key=lambda r: (room(r, threshold, weekly_threshold), left(r) or 0))
        return Decision("switch", f"{active.account} reached its line; {best.account} has "
                        f"{room(best, threshold, weekly_threshold):.0f} points before its lines",
                        active.account, best.account)
    if any(room(r, threshold, weekly_threshold) > 0 for r in candidates):
        return Decision("blocked", "No login below its lines has enough additional room", active.account)
    best = max(candidates, key=lambda r: left(r) or 0)
    now_left, best_left = left(active) or 0, left(best) or 0
    if best_left >= min(2 * now_left, now_left + 3) and best_left > now_left:
        return Decision("switch", f"every login is over its lines; {best.account} has the most left "
                        f"({best_left:.0f}% vs {now_left:.0f}%)", active.account, best.account)
    return Decision("blocked", "every login is over its lines and none has meaningfully more left", active.account)


def _state_path(root: Path) -> Path:
    return root / "codex" / STATE_NAME


def last_switch(root: Path | None = None) -> float:
    path = _state_path(home() if root is None else root)
    try:
        try:
            data = json.loads(read_text_with_retry(path))
        except FileNotFoundError:
            data = json.loads(read_text_with_retry(path.with_name(LEGACY_STATE_NAME)))
        stamp = data.get("switched_at")
        return float(stamp) if type(stamp) in (int, float) and math.isfinite(stamp) else 0.0
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


def record_switch(root: Path | None = None, at: float | None = None) -> None:
    path = _state_path(home() if root is None else root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps({"switched_at": time.time() if at is None else at}))


def tick(*, threshold: float = 90, weekly_threshold: float | None = None, dry_run: bool = False,
         cooldown: float = COOLDOWN_S, root: Path | None = None) -> Decision:
    """One check: read fresh quota, decide, and switch unless cooling down or dry-run."""
    from .codex import CodexAccounts, codex_quota
    from .quota import collect_quota, invalidate_quota

    root = home() if root is None else root
    store = CodexAccounts(root)
    roster, _ = store.roster()
    if dry_run:
        rows = [codex_quota(account, store, refresh_login=False) for account in roster]
    else:
        rows = collect_quota(("codex",), refresh=True, root=root)
    decision = decide(rows, threshold, weekly_threshold)
    if decision.kind != "switch":
        return decision
    if dry_run:
        return Decision("no-switch", f"dry-run: would switch to {decision.target}; " + decision.reason,
                        decision.source, decision.target)
    source = next((account for account in roster if account.active and account.label == decision.source), None)
    target = next((account for account in roster if not account.active and account.label == decision.target), None)
    if source is None or target is None:
        return Decision("blocked", "Codex logins changed during the quota check", decision.source)
    with FileLock(_state_path(root).with_suffix(".lock")):
        since = time.time() - last_switch(root)
        if since < cooldown:
            return Decision("no-switch", f"cooling down after a switch ({cooldown - since:.0f}s left)", decision.source)
        store.switch(target.label, expected_identity=target.login.identity, expected_active=source.login.identity)
        record_switch(root)
    invalidate_quota("codex", root)
    return decision
