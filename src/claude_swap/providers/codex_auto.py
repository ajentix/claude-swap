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
from dataclasses import dataclass, replace
from pathlib import Path

from claude_swap.fsutil import atomic_write_text, read_text_with_retry

from .common import ProviderAccountQuota, QuotaWindow, home
from .codex_policy import Policy, decision_lock, load_policies

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
    assessments: tuple[Assessment, ...] = ()

    @property
    def exit_code(self) -> int:
        return {"switch": SWITCHED, "no-switch": NOTHING, "blocked": BLOCKED}.get(self.kind, ERROR)


@dataclass(frozen=True)
class Assessment:
    account: str
    priority: int
    held: bool
    room: float | None
    remaining: float | None
    result: str


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
           *, margin: float = MARGIN_PCT, policies: dict[str, Policy] | None = None) -> Decision:
    weekly_threshold = threshold if weekly_threshold is None else weekly_threshold
    if not all(math.isfinite(line) and 0 <= line <= 100 for line in (threshold, weekly_threshold)):
        return Decision("error", "Thresholds must be between 0 and 100 percent")
    active = next((r for r in rows if r.active), None)
    if active is None:
        return Decision("error", "no active Codex login")
    policies = policies or {}

    def policy(row: ProviderAccountQuota) -> Policy:
        return policies.get(row.login_ref or "", Policy())

    active_room = room(active, threshold, weekly_threshold)
    assessments = []
    for row in rows:
        account_policy = policy(row)
        headroom = room(row, threshold, weekly_threshold)
        if row.active:
            result = "active"
        elif account_policy.held:
            result = "held"
        elif not row.switchable or row.status != "ok" or headroom is None:
            result = "quota unavailable"
        elif headroom <= 0:
            result = "at or over line"
        elif active_room is not None and headroom - active_room < margin:
            result = "insufficient improvement"
        else:
            result = "eligible"
        assessments.append(Assessment(row.account, account_policy.priority, account_policy.held,
                                      headroom, left(row), result))

    def decision(kind: str, reason: str, target: str | None = None) -> Decision:
        assessed = tuple(replace(item, result="selected") if target is not None and item.account == target
                         else item for item in assessments)
        return Decision(kind, reason, active.account, target, assessed)

    if active.status != "ok" or active_room is None:
        return decision("no-switch", f"{active.account}: usage unknown ({active.error or active.status})")
    if active_room > 0:
        return decision("no-switch", f"{active.account} under its lines ({active_room:.0f} points left)")
    candidates = [r for r in rows if not r.active and r.switchable and r.status == "ok"
                  and not policy(r).held and room(r, threshold, weekly_threshold) is not None]
    if not candidates:
        return decision("blocked", f"{active.account} reached its line and no saved login is eligible")
    under = [r for r in candidates if room(r, threshold, weekly_threshold) > 0
             and room(r, threshold, weekly_threshold) - active_room >= margin]
    if under:
        best = max(under, key=lambda r: (policy(r).priority, room(r, threshold, weekly_threshold), left(r) or 0))
        return decision("switch", f"{active.account} reached its line; {best.account} has "
                        f"{room(best, threshold, weekly_threshold):.0f} points before its lines "
                        f"(priority {policy(best).priority})", best.account)
    if any(room(r, threshold, weekly_threshold) > 0 for r in candidates):
        return decision("blocked", "No login below its lines has enough additional room")
    now_left = left(active) or 0
    rescue = [r for r in candidates if (left(r) or 0) > now_left
              and (left(r) or 0) >= min(2 * now_left, now_left + 3)]
    if rescue:
        best = max(rescue, key=lambda r: (policy(r).priority, left(r) or 0))
        return decision("switch", f"{best.account} has more quota left "
                        f"({left(best):.0f}% vs {now_left:.0f}%, priority {policy(best).priority})", best.account)
    return decision("blocked", "every login is over its lines and none has meaningfully more left")


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
    from .codex import CodexAccounts
    from .quota import collect_quota, invalidate_quota

    root = home() if root is None else root
    store = CodexAccounts(root)
    roster, error = store.roster()
    accounts = {(account.label, account.login.identity, account.active) for account in roster}
    rows = collect_quota(("codex",), root=root, refresh_login=not dry_run, codex_accounts=(roster, error))
    rows = [r for r in rows if (r.account, r.login_ref, r.active) in accounts]
    policies = load_policies(root)
    decision = decide(rows, threshold, weekly_threshold, policies=policies)
    if decision.kind != "switch":
        return decision
    since = time.time() - last_switch(root)
    if since < cooldown:
        return replace(decision, kind="no-switch", target=None,
                       reason=f"cooling down after a switch ({cooldown - since:.0f}s left)")
    if dry_run:
        return replace(decision, kind="no-switch", reason=f"dry-run: would switch to {decision.target}. " + decision.reason)
    source = next((account for account in roster if account.active and account.label == decision.source), None)
    target = next((account for account in roster if not account.active and account.label == decision.target), None)
    if source is None or target is None:
        return Decision("blocked", "Codex logins changed during the quota check", decision.source)
    with decision_lock(root):
        since = time.time() - last_switch(root)
        if since < cooldown:
            return Decision("no-switch", f"cooling down after a switch ({cooldown - since:.0f}s left)", decision.source)
        if load_policies(root) != policies:
            return replace(decision, kind="blocked", target=None, reason="Codex account policies changed")
        store.switch(target.label, expected_identity=target.login.identity, expected_active=source.login.identity)
        record_switch(root)
    invalidate_quota("codex", root)
    return decision
