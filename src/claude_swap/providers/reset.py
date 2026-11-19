"""Explicit redemption of one verified Claude or Codex subscription credit."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher

from .api import _credential, coding_plan_config, get_usage, post_reset
from .claude import claude_headers
from .codex import CREDIT_HEADERS, USAGE_URL, CodexAccounts, codex_oauth
from .common import KST, ProviderError, ResetCredits, display_text, home, object_dict, read_json, reset_clock, write_json
from .quota import invalidate_quota
from .reset_credits import (CLAUDE_USAGE_URL, CODEX_CREDITS_URL, ZAI_CREDIT_BASE, ZAI_CREDITS_URL,
                            ResetOffer, claude_reset_offers, codex_reset_offers, zai_reset_offers)

RESET_PROVIDERS = ("claude", "codex", "zai-glm")
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
    inventory: dict | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ResetIntent:
    target: ResetTarget
    offer: ResetOffer
    observed_at: float
    request_id: str | None = field(default=None, repr=False)

    def prompt(self) -> str:
        account = f"{self.target.provider} {self.target.label}"
        if self.target.number != self.target.label:
            account += f" ({self.target.number})"
        scope = f" Applies to {', '.join(self.offer.windows)}." if self.offer.windows else ""
        if self.request_id:
            return f"Retry the previous reset request for {account}?{scope}"
        expiry = self.offer.expires_at
        note = f" Credit expires {reset_clock(expiry, datetime.now(KST))} KST." if expiry else ""
        return f"Use one reset credit for {account}?{scope}{note}"


@dataclass(frozen=True)
class ResetOptions:
    target: ResetTarget
    observed_at: float
    credits: ResetCredits | None
    offers: tuple[ResetOffer, ...] = ()
    pending: ResetIntent | None = None
    warning: str | None = None
    pending_state: str | None = None

    def choose(self, index: int) -> ResetIntent:
        if self.pending_state:
            raise ProviderError("A reset request is pending for this workspace. Use --retry for the same request")
        if not 1 <= index <= len(self.offers) or not self.offers[index - 1].usable:
            raise ProviderError("The selected reset credit is not currently available to use")
        return ResetIntent(self.target, self.offers[index - 1], self.observed_at)


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
    headers = claude_headers(credentials, cli_identity=True)  # the inventory and redeem need the CLI identity
    profile = get_usage(CLAUDE_PROFILE_URL, headers)
    live_org = _uuid(object_dict(profile.get("organization")).get("uuid"))
    account = object_dict(profile.get("account"))
    live_email = account.get("email")
    if not live_org or live_org != _uuid(org_uuid) or not isinstance(live_email, str) or live_email.casefold() != email.casefold():
        raise ProviderError("Claude OAuth profile does not match the selected account")
    identity = f"{live_org}:{email.casefold()}"
    label = display_text(f"{alias} ({email})" if alias else email,
                         headers["Authorization"].removeprefix("Bearer ")) or "account"
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


def _zai_target(identifier: str, root: Path) -> ResetTarget:
    _, settings, _ = coding_plan_config(root, "zai-glm")
    matches = []
    for label, fields in settings.items():
        key = _credential(fields, "api_key")
        name = display_text(label, key, _credential(fields, "session")) or "account"
        if name.casefold() == identifier.casefold() and key:
            matches.append((name, key))
    if len(matches) != 1:
        raise ProviderError("Select a configured z.ai account name from cswap quota")
    label, key = matches[0]
    headers = {"Authorization": f"Bearer {key}"}
    response = get_usage(ZAI_CREDITS_URL, headers)
    block = object_dict(response.get("data"))
    customer = block.get("customerId")
    if (type(customer) is not int and (not isinstance(customer, str) or not customer.isdecimal())
            or not customer or block.get("targetType") != "PERSONAL"):
        raise ProviderError("The z.ai reset inventory did not verify a personal account")
    scope = str(customer)
    return ResetTarget("zai-glm", label, label, scope, scope, headers, response)


def _target(provider: str, identifier: str, root: Path, switcher: ClaudeAccountSwitcher | None) -> ResetTarget:
    if provider == "claude":
        return _claude_target(identifier, switcher if switcher is not None else ClaudeAccountSwitcher())
    if provider == "codex":
        return _codex_target(identifier, root)
    if provider == "zai-glm":
        return _zai_target(identifier, root)
    raise ProviderError("This provider has no supported reset credit endpoint")


def _inventory(target: ResetTarget) -> tuple[ResetCredits | None, list[ResetOffer]]:
    if target.provider == "claude":
        return claude_reset_offers(get_usage(CLAUDE_USAGE_URL, target.headers))
    if target.provider == "zai-glm":
        return zai_reset_offers(target.inventory or {})
    return codex_reset_offers(get_usage(CODEX_CREDITS_URL, {**target.headers, **CREDIT_HEADERS}))


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


def reset_options(provider: str, identifier: str, *, root: Path | None = None,
                  switcher: ClaudeAccountSwitcher | None = None, inventory: bool = True) -> ResetOptions:
    root = home() if root is None else root
    observed = time.time()
    target = _target(provider, identifier, root, switcher)
    entry = _read_journal(_journal_path(root, target))
    if _pending(entry):
        owner = entry.get("owner") == _owner(target)
        windows = entry.get("windows")
        scopes = tuple(value for value in windows if isinstance(value, str)) if isinstance(windows, list) else ()
        pending = (ResetIntent(target, ResetOffer(entry["credit_id"], windows=scopes), observed, entry["request_id"])
                   if owner else None)
        state = "sending" if entry["outcome"] == "sending" and observed - entry["updated_at"] < RETRY_WAIT else "unknown"
        return ResetOptions(target, observed, None, pending=pending, pending_state=state,
                            warning="Another account has a pending reset for this workspace" if not owner else None)
    credits, offers = _inventory(target) if inventory else (None, [])
    return ResetOptions(target, observed, credits, tuple(offers))


def prepare_reset(provider: str, identifier: str, *, retry: bool = False, credit: int | None = None,
                  root: Path | None = None, switcher: ClaudeAccountSwitcher | None = None) -> ResetIntent:
    options = reset_options(provider, identifier, root=root, switcher=switcher, inventory=not retry)
    if retry:
        if options.pending is None:
            raise ProviderError("No pending reset request for this account")
        if options.pending_state == "sending":
            raise ProviderError("The reset request may still be in progress. Check again in a minute")
        return options.pending
    if credit is not None or options.pending_state:
        return options.choose(1 if credit is None else credit)
    if options.credits is None:
        raise ProviderError("Reset credit availability was not reported by the provider")
    for index, offer in enumerate(options.offers, 1):
        if offer.usable:
            return options.choose(index)
    raise ProviderError("The provider has no reset credit currently available to use" if options.credits.available_count
                        else "No reset credits available")


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
            entry = {"request_id": secrets.token_hex(16), "credit_id": intent.offer.credit_id,
                     "owner": _owner(target), "created_at": now, "updated_at": now, "outcome": "sending"}
            entry["windows"] = list(intent.offer.windows)
            if target.provider == "zai-glm":
                entry.update(reset_type=intent.offer.reset_type, grant_type=intent.offer.grant_type)
        write_json(path, entry)
        return entry


def _send(target: ResetTarget, entry: dict) -> str:
    try:
        if target.provider == "zai-glm":
            reset_type, grant = entry.get("reset_type"), entry.get("grant_type")
            if reset_type not in ("WEEK", "FIVE_HOUR") or not isinstance(grant, str) or not re.fullmatch(r"[A-Z_]{1,40}", grant):
                return "unknown"
            data = post_reset(f"{ZAI_CREDIT_BASE}/use", target.headers, {
                "recordId": int(entry["credit_id"]), "targetType": "PERSONAL", "resetType": reset_type,
                "requestId": entry["request_id"], "grantType": grant,
            })
            return "reset" if data.get("success") is True and data.get("code") in (0, 200) else "unknown"
        if target.provider == "codex":
            data = post_reset(CODEX_CREDITS_URL + "/consume", {**target.headers, **CREDIT_HEADERS}, {
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
    if not intent.request_id:
        _, offers = _inventory(target)
        if not any(offer.usable and offer == intent.offer for offer in offers):
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
