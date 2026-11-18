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

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


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
            seconds = entry.get("limit_window_seconds")
            label = {18000: "5h", 604800: "7d"}.get(seconds, default) if isinstance(seconds, int) else default
            reset = entry.get("reset_at", entry.get("resets_at"))
            if reset is None and type(entry.get("reset_after_seconds")) in (float, int):
                reset = time.time() + entry["reset_after_seconds"]
            windows.append(window(prefix + label, entry.get("used_percent"), reset, offset=100, scale=-1))
    return tuple(windows), display_text(data.get("plan_type"), *secrets)


def codex_quota(account: Account) -> ProviderAccountQuota:
    row = ProviderAccountQuota("codex", account.label, account.number, account.active, True)
    tokens = object_dict(account.login.data.get("tokens"))
    access = tokens.get("access_token")
    if account.login.data.get("auth_mode") == "apikey" or not isinstance(access, str) or not access:
        return replace(row, status="unavailable", error="API-key login has no ChatGPT subscription quota")
    account_id = tokens.get("account_id")
    if not account_id:
        auth = object_dict(jwt_claims(access).get("https://api.openai.com/auth"))
        account_id = auth.get("chatgpt_account_id")
    headers = {"Authorization": f"Bearer {access}"}
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        windows, plan = parse_codex(get_usage(USAGE_URL, headers), account.login.secrets)
        if not any(item.remaining_pct is not None for item in windows):
            raise ProviderError("Codex returned no quota windows")
        return replace(row, windows=windows, plan=plan)
    except ProviderError as exc:
        return replace(row, status="error", error=str(exc))
