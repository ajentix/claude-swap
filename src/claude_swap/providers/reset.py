"""Explicit redemption of one verified Claude or Codex subscription credit."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher

from .api import get_usage, post_reset
from .claude import claude_headers
from .codex import USAGE_URL, CodexAccounts, codex_oauth
from .common import KST, ProviderError, display_text, home, object_dict, read_json, reset_clock, write_json
from .quota import invalidate_quota
from .reset_credits import CLAUDE_USAGE_URL, CODEX_CREDITS_URL, ResetOffer, claude_reset_inventory, codex_reset_inventory

RESET_PROVIDERS = ("claude", "codex")
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_RESET_URL = "https://api.anthropic.com/api/organizations/{organization}/reset_rate_limits"
RETRY_WAIT = 60
UNKNOWN_MAX_AGE = 24 * 60 * 60

OUTCOMES = {
    "reset": "Quota reset confirmed",
    "already_redeemed": "Provider reports the selected credit was already redeemed",
    "already_used": "Provider reports the selected request was already used",
    "no_credit": "Provider reports no credit for this request",
    "nothing_to_reset": "Provider reports no usage to reset",
    "not_limited": "Claude requires a usage limit to be reached",
    "cooldown": "Claude reports a reset cooldown",
    "ineligible": "Claude reports the account is ineligible",
    "unknown": "Reset outcome unknown. Check quota or retry the same request with --retry",
}


@dataclass(frozen=True)
class ResetTarget:
    provider: str
    number: str
    label: str
    identity: str = field(repr=False)
    scope: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class ResetIntent:
    target: ResetTarget
    offer: ResetOffer
    observed_at: float
    request_id: str | None = field(default=None, repr=False)

    def prompt(self) -> str:
        account = f"{self.target.provider} {self.target.label} ({self.target.number})"
        if self.request_id:
            return f"Retry the previous reset request for {account}?"
        expiry = self.offer.expires_at
        note = f" Credit expires {reset_clock(expiry, datetime.now(KST))} KST." if expiry else ""
        return f"Use one reset credit for {account}?{note}"


def _uuid(value: object) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _claude_target(identifier: str, switcher: ClaudeAccountSwitcher) -> ResetTarget:
    number = switcher._resolve_account_identifier(identifier)
    accounts = switcher._build_accounts_info()
    entry = next((row for row in accounts if str(row[0]) == str(number)), None)
    if entry is None:
        raise ProviderError("Claude account not found")
    _, email, _, org_uuid, _, credentials, alias = entry
    headers = claude_headers(credentials)
    profile = get_usage(CLAUDE_PROFILE_URL, headers)
    live_org = _uuid(object_dict(profile.get("organization")).get("uuid"))
    account = object_dict(profile.get("account"))
    live_email = account.get("email")
    if not live_org or live_org != _uuid(org_uuid) or not isinstance(live_email, str) or live_email.casefold() != email.casefold():
        raise ProviderError("Claude OAuth profile does not match the selected account")
    identity = f"{live_org}:{email.casefold()}"
    label = display_text(alias or email, headers["Authorization"].removeprefix("Bearer ")) or "account"
    return ResetTarget("claude", str(number), label, identity, live_org, headers)


def _codex_target(identifier: str, root: Path) -> ResetTarget:
    account, _ = CodexAccounts(root)._resolve(identifier)
    workspace_id, headers = codex_oauth(account)
    if not workspace_id:
        raise ProviderError("Codex login has no verified ChatGPT workspace ID")
    raw = get_usage(USAGE_URL, headers)
    if raw.get("account_id") != workspace_id:
        raise ProviderError("ChatGPT usage response did not verify the selected workspace")
    return ResetTarget("codex", account.number, account.label, account.login.identity, workspace_id, headers)


def _target(provider: str, identifier: str, root: Path, switcher: ClaudeAccountSwitcher | None) -> ResetTarget:
    if provider == "claude":
        return _claude_target(identifier, switcher if switcher is not None else ClaudeAccountSwitcher())
    if provider == "codex":
        return _codex_target(identifier, root)
    raise ProviderError("Reset credits are supported for Claude and Codex")


def _offer(target: ResetTarget) -> ResetOffer:
    if target.provider == "claude":
        credits, offer = claude_reset_inventory(get_usage(CLAUDE_USAGE_URL, target.headers))
    else:
        credits, offer = codex_reset_inventory(get_usage(CODEX_CREDITS_URL, target.headers))
    if credits is None:
        raise ProviderError("Reset credit availability was not reported by the provider")
    if not offer:
        if credits.available_count:
            raise ProviderError("The provider has no reset credit currently available to use")
        raise ProviderError("No reset credits available")
    return offer


def _journal_path(root: Path, target: ResetTarget) -> Path:
    scope = hashlib.sha256(f"{target.provider}:{target.scope}".encode()).hexdigest()
    return root / "reset_requests" / f"{scope}.json"


def _read_journal(path: Path) -> dict:
    entry = read_json(path)
    if entry is None:
        return {}
    if not (
        type(entry.get("updated_at")) in (int, float)
        and type(entry.get("created_at")) in (int, float)
        and isinstance(entry.get("request_id"), str)
        and isinstance(entry.get("credit_id"), str)
        and isinstance(entry.get("owner"), str)
        and entry.get("outcome") in (*OUTCOMES, "sending")
    ):
        raise ProviderError("The local reset request record is unreadable")
    return entry


def _pending(entry: dict) -> bool:
    return entry.get("outcome") in ("sending", "unknown") and time.time() - entry["created_at"] < UNKNOWN_MAX_AGE


def _owner(target: ResetTarget) -> str:
    return hashlib.sha256(target.identity.encode()).hexdigest()


def prepare_reset(provider: str, identifier: str, *, retry: bool = False, root: Path | None = None,
                  switcher: ClaudeAccountSwitcher | None = None) -> ResetIntent:
    root = home() if root is None else root
    observed = time.time()
    target = _target(provider, identifier, root, switcher)
    entry = _read_journal(_journal_path(root, target))
    if retry:
        if not _pending(entry) or entry.get("owner") != _owner(target):
            raise ProviderError("No pending reset request for this account")
        return ResetIntent(target, ResetOffer(entry["credit_id"]), observed, entry["request_id"])
    if _pending(entry):
        raise ProviderError("A reset request is pending for this workspace. Use --retry for the same request")
    return ResetIntent(target, _offer(target), observed)


def _reserve(path: Path, target: ResetTarget, intent: ResetIntent) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(path.with_suffix(".lock")):
        previous = _read_journal(path)
        now = time.time()
        if intent.request_id:
            if not _pending(previous) or previous.get("request_id") != intent.request_id or previous.get("owner") != _owner(target):
                raise ProviderError("The pending reset request changed. Check quota again")
            if previous["outcome"] == "sending" and now - previous["updated_at"] < RETRY_WAIT:
                raise ProviderError("The reset request may still be in progress. Check again in a minute")
            entry = {**previous, "updated_at": now, "outcome": "sending"}
        else:
            if _pending(previous) or previous.get("updated_at", 0) >= intent.observed_at:
                raise ProviderError("Another reset request changed this workspace. Check quota again")
            entry = {"request_id": secrets.token_hex(32), "credit_id": intent.offer.credit_id,
                     "owner": _owner(target), "created_at": now, "updated_at": now, "outcome": "sending"}
        write_json(path, entry)
        return entry


def _send(target: ResetTarget, entry: dict) -> str:
    try:
        if target.provider == "codex":
            data = post_reset(CODEX_CREDITS_URL + "/consume", target.headers, {
                "redeem_request_id": entry["request_id"], "credit_id": entry["credit_id"],
            })
            outcome = data.get("code")
            known = ("reset", "already_redeemed", "no_credit", "nothing_to_reset")
        else:
            data = post_reset(CLAUDE_RESET_URL.format(organization=target.scope), target.headers, {
                "program": "cedar_ember", "grant_id": entry["credit_id"], "request_id": entry["request_id"],
            })
            if data.get("reason") in ("stamp_indeterminate", "reset_unconfirmed"):
                return "unknown"
            outcome = data.get("result")
            known = ("reset", "already_used", "not_limited", "cooldown", "ineligible")
        return outcome if outcome in known else "unknown"
    except (ProviderError, OSError, ValueError):
        return "unknown"


def redeem_reset(intent: ResetIntent, *, root: Path | None = None,
                 switcher: ClaudeAccountSwitcher | None = None) -> str:
    root = home() if root is None else root
    target = _target(intent.target.provider, intent.target.number, root, switcher)
    if (target.scope, target.identity) != (intent.target.scope, intent.target.identity):
        raise ProviderError("The selected account changed. Review the reset again")
    if not intent.request_id and _offer(target) != intent.offer:
        raise ProviderError("The reset credit changed. Review the reset again")
    path = _journal_path(root, target)
    entry = _reserve(path, target, intent)
    try:
        outcome = _send(target, entry)
        try:
            with FileLock(path.with_suffix(".lock")):
                if _read_journal(path) == entry:
                    write_json(path, {
                        **entry, "outcome": outcome, "updated_at": time.time(),
                        "credit_id": entry["credit_id"] if outcome == "unknown" else "",
                    })
        except (OSError, LockError, ValueError, ProviderError):
            outcome = "unknown"
    finally:
        invalidate_quota(target.provider, root)
    return outcome
