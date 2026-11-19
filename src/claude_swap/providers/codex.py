"""Direct Codex CLI logins and ChatGPT subscription usage."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

from .accounts import Account, LocalAccounts, Login
from .api import get_usage
from .common import (
    ProviderAccountQuota, ProviderError, child_environment, display_text, email_label,
    jwt_claims, object_dict, objects, read_json, window, write_json,
)
from .reset_credits import CODEX_CREDITS_URL, codex_reset_inventory

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# The reset-credit detail call wants the headers the Codex desktop app sends
# (openai/codex, CodexBar, OpenUsage).
CREDIT_HEADERS = {"OpenAI-Beta": "codex-1", "originator": "Codex Desktop"}


def window_label(seconds: object, default: str) -> str:
    """A window's name from its length: 18000 -> "5h", 604800 -> "7d",
    2592000 -> "30d" (Free plans); the slot's default only when no length came."""
    if type(seconds) is not int or seconds <= 0:
        return default
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, round(seconds / 60))}m"


class CodexAccounts(LocalAccounts):
    provider = "codex"
    command = "codex"
    relative_auth = Path("auth.json")

    @property
    def active_path(self) -> Path:
        return Path.home() / ".codex" / self.relative_auth

    def parse(self, data: dict) -> Login:
        tokens = object_dict(data.get("tokens"))
        key = data.get("OPENAI_API_KEY")
        access = tokens.get("access_token")
        secrets = tuple(s for s in (access, tokens.get("refresh_token"), tokens.get("id_token"), key)
                        if isinstance(s, str) and s)
        if data.get("auth_mode") in (None, "chatgpt") and isinstance(access, str) and access:
            id_claims = jwt_claims(tokens.get("id_token"))
            access_claims = jwt_claims(access)
            profile = object_dict(access_claims.get("https://api.openai.com/profile"))
            auth = object_dict(access_claims.get("https://api.openai.com/auth"))
            email = email_label(id_claims.get("email")) or email_label(profile.get("email"))
            account_id = tokens.get("account_id") or auth.get("chatgpt_account_id")
            principal = id_claims.get("sub") or auth.get("chatgpt_user_id") or email or access_claims.get("sub")
            if isinstance(account_id, str) and account_id and isinstance(principal, str) and principal:
                identity = f"{account_id}:{principal}"
            else:
                fingerprint = tokens.get("refresh_token") or access
                identity = fingerprint if isinstance(fingerprint, str) else access
            digest = hashlib.sha256(identity.encode()).hexdigest()
            label = display_text(email, *secrets) or f"codex-{digest[:8]}"
            return Login(digest, label, data, secrets)
        if data.get("auth_mode") in (None, "apikey") and isinstance(key, str) and key:
            digest = hashlib.sha256(key.encode()).hexdigest()
            return Login(digest, f"api-key-{digest[:8]}", data, secrets)
        raise ProviderError("No supported direct Codex login in auth.json")

    def read_active(self) -> Login | None:
        data = read_json(self.active_path)
        return self.parse(data) if data is not None else None

    def write_active(self, login: Login) -> None:
        write_json(self.active_path, login.data)

    def environment(self, profile: Path) -> dict[str, str]:
        env = child_environment()
        env.update(CODEX_HOME=str(profile), HOME=str(Path.home()), USERPROFILE=str(Path.home()))
        return env

    def run(self, identifier: str, args: list[str]) -> int:
        return super().run(identifier, ["-c", 'cli_auth_credentials_store="file"', *args])


def parse_codex(data: dict, secrets: tuple[str, ...]) -> tuple:
    limits = [("", object_dict(data.get("rate_limit")))]
    for item in objects(data.get("additional_rate_limits")):
        name = display_text(item.get("limit_name") or item.get("metered_feature"), *secrets)
        if name:
            limits.append((f"{name} ", object_dict(item.get("rate_limit"))))
    windows = []
    for prefix, limit in limits:
        for key, default in (("primary_window", "5h"), ("secondary_window", "7d")):
            entry = object_dict(limit.get(key))
            if not entry:
                continue
            label = window_label(entry.get("limit_window_seconds"), default)
            reset = entry.get("reset_at", entry.get("resets_at"))
            if reset is None and type(entry.get("reset_after_seconds")) in (float, int):
                reset = time.time() + entry["reset_after_seconds"]
            windows.append(window(prefix + label, entry.get("used_percent"), reset, offset=100, scale=-1))
    # Team/Enterprise: a monthly credit pool instead of (or beside) the windows.
    spend = object_dict(object_dict(data.get("spend_control")).get("individual_limit")) \
        or object_dict(data.get("individual_limit"))
    if spend.get("remaining_percent") is not None:
        windows.append(window("credits", spend.get("remaining_percent"),
                              spend.get("resets_at", spend.get("reset_at"))))
    return tuple(windows), display_text(data.get("plan_type"), *secrets)


def codex_oauth(account: Account) -> tuple[str | None, dict[str, str]]:
    tokens = object_dict(account.login.data.get("tokens"))
    access = tokens.get("access_token")
    if account.login.data.get("auth_mode") == "apikey" or not isinstance(access, str) or not access:
        raise ProviderError("API-key login has no ChatGPT subscription quota")
    account_id = tokens.get("account_id")
    if not account_id:
        auth = object_dict(jwt_claims(access).get("https://api.openai.com/auth"))
        account_id = auth.get("chatgpt_account_id")
    headers = {"Authorization": f"Bearer {access}"}
    if isinstance(account_id, str) and account_id and account_id.isascii() and account_id.isprintable():
        headers["ChatGPT-Account-Id"] = account_id
        return account_id, headers
    return None, headers


def codex_quota(account: Account) -> ProviderAccountQuota:
    row = ProviderAccountQuota("codex", account.label, account.number, account.active, True)
    try:
        _, headers = codex_oauth(account)
    except ProviderError as exc:
        return replace(row, status="unavailable", error=str(exc))
    try:
        raw = get_usage(USAGE_URL, headers)
        windows, plan = parse_codex(raw, account.login.secrets)
        credits, _ = codex_reset_inventory(object_dict(raw.get("rate_limit_reset_credits")))
        if credits and credits.available_count and credits.next_expiry is None:
            try:
                details, _ = codex_reset_inventory(get_usage(CODEX_CREDITS_URL, {**headers, **CREDIT_HEADERS}, timeout=10))
                credits = details or credits
            except ProviderError:
                pass  # The primary usage read still has the reported credit count.
        if credits is None and not any(item.remaining_pct is not None for item in windows):
            raise ProviderError("Codex returned no quota windows")
        return replace(row, windows=windows, plan=plan, reset_credits=credits)
    except ProviderError as exc:
        return replace(row, status="error", error=str(exc))
