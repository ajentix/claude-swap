"""Direct Codex CLI logins and ChatGPT subscription usage."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock

from .accounts import Account, LocalAccounts, Login
from .api import NoRedirect, get_usage
from .common import (
    ProviderAccountQuota, ProviderError, child_environment, display_text, email_label,
    home, jwt_claims, object_dict, objects, read_json, window, write_json,
)
from .codex_auto import LEGACY_STATE_NAME, STATE_NAME
from .reset_credits import CODEX_CREDITS_URL, codex_reset_inventory

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# The reset-credit detail call wants the headers the Codex desktop app sends
# (openai/codex, CodexBar, OpenUsage).
CREDIT_HEADERS = {"OpenAI-Beta": "codex-1", "originator": "Codex Desktop"}

# Token refresh, as the Codex CLI does it (openai/codex codex-rs/login): a JSON
# POST with its public client id; tokens rotate, so a refresh token works once.
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_AFTER_S = 8 * 86400  # the CLI's TOKEN_REFRESH_INTERVAL
EXPIRY_SKEW_S = 300
RELOGIN_CODES = {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated", "invalid_grant"}
RELOGIN = "Codex login expired. Sign in with codex, then run cswap codex add"


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
            email = email_label(id_claims.get("email")) or email_label(profile.get("email"))
            identity = _stable_identity(tokens)
            if identity is None:
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

    def stash_paths(self) -> list[Path]:
        paths = []
        for path in super().stash_paths():
            if path.name.casefold() in {STATE_NAME, ".account-policy.json"}:
                continue
            if path.name.casefold() == LEGACY_STATE_NAME:
                try:
                    data = read_json(path)
                except (OSError, ValueError):
                    pass  # Let the roster report an unreadable file.
                else:
                    if data and set(data) == {"switched_at"}:
                        continue
            paths.append(path)
        return paths

    def environment(self, profile: Path) -> dict[str, str]:
        env = child_environment()
        env.update(CODEX_HOME=str(profile), HOME=str(Path.home()), USERPROFILE=str(Path.home()))
        return env

    def run(self, identifier: str, args: list[str]) -> int:
        account, _ = self._resolve(identifier)
        with self._login_gate(account):
            selected, _ = self._resolve(account.label)
            if selected.login.identity != account.login.identity:
                raise ProviderError("The selected login changed. List accounts and try again")
            return self._run_account(selected, ["-c", 'cli_auth_credentials_store="file"', *args])

    def _login_gate(self, account: Account) -> FileLock:
        return FileLock(self.directory / f".login-{account.login.identity}.lock")

    def switch(self, identifier: str, *, expected_identity: str | None = None,
               expected_active: str | None = None) -> str:
        account, _ = self._resolve(identifier)
        if expected_identity is not None and expected_identity != account.login.identity:
            raise ProviderError("The selected login changed. List accounts and try again")
        with self._login_gate(account):
            return super().switch(account.label, expected_identity=account.login.identity,
                                  expected_active=expected_active)


def parse_codex(data: dict, secrets: tuple[str, ...]) -> tuple:
    limits = [("", object_dict(data.get("rate_limit")))]
    for item in objects(data.get("additional_rate_limits")):
        name = display_text(item.get("limit_name") or item.get("metered_feature"), *secrets)
        if name:
            limits.append((f"{name} ", object_dict(item.get("rate_limit"))))
    windows = []
    for prefix, limit in limits:
        blocked = limit.get("allowed") is False or limit.get("limit_reached") is True
        start = len(windows)
        for key, default in (("primary_window", "5h"), ("secondary_window", "7d")):
            entry = object_dict(limit.get(key))
            if not entry:
                continue
            label = window_label(entry.get("limit_window_seconds"), default)
            reset = entry.get("reset_at", entry.get("resets_at"))
            if reset is None and type(entry.get("reset_after_seconds")) in (float, int):
                reset = time.time() + entry["reset_after_seconds"]
            windows.append(window(prefix + label, entry.get("used_percent"), reset,
                                  offset=100, scale=-1, disabled=blocked))
        if blocked and not prefix and len(windows) == start:
            windows.append(window("Codex", None, disabled=True))
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


def _refresh_due(data: dict, now: float) -> bool:
    tokens = object_dict(data.get("tokens"))
    exp = jwt_claims(tokens.get("access_token")).get("exp")
    if type(exp) in (int, float) and exp - now < EXPIRY_SKEW_S:
        return True
    try:
        last = datetime.fromisoformat(str(data.get("last_refresh")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return now - last > REFRESH_AFTER_S


def _post_refresh(refresh_token: str) -> dict:
    body = json.dumps({"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token}).encode()
    request = Request(REFRESH_URL, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "claude-swap"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=45) as response:
            data = json.loads(response.read(1_048_576))
    except HTTPError as exc:
        with exc:
            try:
                err = object_dict(json.loads(exc.read(4096)))
            except (OSError, ValueError):
                err = {}
        detail = object_dict(err.get("error")) if isinstance(err.get("error"), dict) else err
        code = detail.get("code") or err.get("error")
        if exc.code == 401 or (isinstance(code, str) and code in RELOGIN_CODES):
            raise ProviderError(RELOGIN) from None
        raise ProviderError(f"Codex token refresh failed (HTTP {exc.code})") from None
    except (URLError, OSError, ValueError):
        raise ProviderError("Codex token refresh unavailable") from None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ProviderError("Codex token refresh returned no token")
    return data


def refresh_saved(account: Account, store: "CodexAccounts") -> Account:
    """Refresh a saved, inactive login and write it back to its own file.

    Never the live ~/.codex login: a running Codex CLI owns that one, and a
    second refresh would burn its single-use refresh token. The file is reread
    under the store lock and only written if it still holds the token we used.
    """
    if account.active or account.path is None:
        return account
    with store._login_gate(account):
        accounts, error = store.roster()
        current = next((row for row in accounts if row.login.identity == account.login.identity
                        and row.path == account.path), None)
        if current is None:
            raise ProviderError("The saved Codex login changed. List accounts and try again")
        if error and store._current()[1]:
            raise ProviderError("Cannot verify the active Codex login before refresh")
        if current.active or current.login != account.login:
            return current
        return _rotate_saved(current, store)


def _rotate_saved(account: Account, store: CodexAccounts) -> Account:
    tokens = object_dict(account.login.data.get("tokens"))
    if not _stable_identity(tokens):
        raise ProviderError("Cannot verify this saved Codex login for refresh. Sign in again")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ProviderError(RELOGIN)
    fresh = _post_refresh(refresh_token)
    with store._lock():
        current = read_json(account.path) or {}
        if current != account.login.data:
            raise ProviderError("The saved Codex login changed during refresh. Try again")
        merged = {**object_dict(current.get("tokens"))}
        for key in ("id_token", "access_token", "refresh_token"):
            if isinstance(fresh.get(key), str) and fresh[key]:
                merged[key] = fresh[key]
        current["tokens"] = merged
        current["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        login = store.parse(current)
        if login.identity != account.login.identity:
            raise ProviderError("Codex token refresh returned another account. Sign in again")
        write_json(account.path, current)
    return replace(account, login=login)


def _stable_identity(tokens: dict) -> str | None:
    id_claims = jwt_claims(tokens.get("id_token"))
    access_claims = jwt_claims(tokens.get("access_token"))
    profile = object_dict(access_claims.get("https://api.openai.com/profile"))
    auth = object_dict(access_claims.get("https://api.openai.com/auth"))
    account_id = tokens.get("account_id") or auth.get("chatgpt_account_id")
    principal = (id_claims.get("sub") or auth.get("chatgpt_user_id")
                 or email_label(id_claims.get("email")) or email_label(profile.get("email"))
                 or access_claims.get("sub"))
    if isinstance(account_id, str) and account_id and isinstance(principal, str) and principal:
        return f"{account_id}:{principal}"
    return None


def codex_quota(account: Account, store: "CodexAccounts | None" = None, *,
                refresh_login: bool = True) -> ProviderAccountQuota:
    row = ProviderAccountQuota("codex", account.label, account.number, account.active, True,
                               login_ref=account.login.identity)
    store = store or CodexAccounts(home())
    try:
        if (refresh_login and not account.active and account.path is not None
                and object_dict(account.login.data.get("tokens"))
                and _refresh_due(account.login.data, time.time())):
            account = refresh_saved(account, store)
        _, headers = codex_oauth(account)
    except LockError:
        return replace(row, status="unavailable", error="Saved Codex login is busy")
    except ProviderError as exc:
        return replace(row, status="unavailable", error=str(exc))
    try:
        try:
            raw = get_usage(USAGE_URL, headers)
        except ProviderError as exc:
            if (not refresh_login or (exc.status_code != 401 and not str(exc).startswith("HTTP 401"))
                    or account.active or account.path is None):
                raise
            account = refresh_saved(account, store)  # one retry with a fresh token
            _, headers = codex_oauth(account)
            raw = get_usage(USAGE_URL, headers)
        windows, plan = parse_codex(raw, account.login.secrets)
        credits, _ = codex_reset_inventory(object_dict(raw.get("rate_limit_reset_credits")))
        if credits and credits.available_count and credits.next_expiry is None:
            try:
                details, _ = codex_reset_inventory(get_usage(CODEX_CREDITS_URL, {**headers, **CREDIT_HEADERS}, timeout=10))
                credits = details or credits
            except ProviderError:
                pass  # The primary usage read still has the reported credit count.
        if credits is None and not any(item.remaining_pct is not None or item.disabled for item in windows):
            raise ProviderError("Codex returned no quota windows")
        return replace(row, windows=windows, plan=plan, reset_credits=credits)
    except LockError:
        return replace(row, status="unavailable", error="Saved Codex login is busy")
    except ProviderError as exc:
        return replace(row, status="error", error=str(exc), retry_at=exc.retry_after, http_status=exc.status_code)
