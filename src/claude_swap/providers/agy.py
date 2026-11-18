"""Antigravity account storage and isolated `/usage` CLI execution."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import agy_keyring
from .accounts import Account, LocalAccounts, Login
from .common import (
    ProviderAccountQuota, ProviderError, child_environment, email_label, jwt_claims,
    object_dict, objects, process_options, read_json, timestamp, window, write_json,
)

USAGE_TIMEOUT = 35
MIN_RAM = int(3.5 * 1024 ** 3)
USAGE_SLOTS = threading.BoundedSemaphore(2)
MAX_OUTPUT = 1024 * 1024


class AgyAccounts(LocalAccounts):
    provider = "agy"
    command = "agy"
    relative_auth = Path(".gemini/antigravity-cli/antigravity-oauth-token")

    def parse(self, data: dict) -> Login:
        raw = object_dict(data.get("token")) or data
        access = raw.get("access_token") or raw.get("accessToken")
        refresh = raw.get("refresh_token") or raw.get("refreshToken")
        id_token = data.get("id_token") or data.get("idToken")
        claim_email = email_label(jwt_claims(id_token).get("email"))
        email = email_label(data.get("id_token_email")) or claim_email
        if email and claim_email and email != claim_email:
            raise ProviderError("Antigravity login has conflicting email fields")
        if not email or not isinstance(access, str) or not access:
            raise ProviderError("Antigravity login is missing an email or access token")
        if data.get("auth_method") not in (None, "consumer") or any(
            data.get(key) for key in ("wif_provider", "saved_wif_provider", "project_id", "region")
        ):
            raise ProviderError("Only direct Google OAuth Antigravity logins are supported")
        token = {**raw} if raw is not data else {}
        token.update(access_token=access, token_type="Bearer")
        if isinstance(refresh, str) and refresh:
            token["refresh_token"] = refresh
        expiry = raw.get("expiry") or raw.get("expires_at") or raw.get("expiresAt")
        if parsed := timestamp(expiry):
            token["expiry"] = parsed
        normalized = dict(data) if raw is not data else {}
        normalized.update(token=token, id_token_email=email, auth_method="consumer")
        if isinstance(id_token, str) and id_token:
            normalized["id_token"] = id_token
        secrets = tuple(item for item in (access, refresh, id_token) if isinstance(item, str) and item)
        if email in secrets:
            raise ProviderError("Antigravity login has an invalid email")
        return Login(email, email, normalized, secrets)

    def stash_paths(self) -> list[Path]:
        legacy = Path.home() / ".nova" / "agy"
        return [*super().stash_paths(), *sorted(legacy.glob("*.json"))]

    def _native(self) -> bytes | None:
        if sys.platform == "win32":
            return agy_keyring.windows_read()
        if sys.platform == "darwin":
            return agy_keyring.mac_read()
        return None

    def read_active(self) -> Login | None:
        native = self._native()
        data = json.loads(native) if native is not None else read_json(Path.home() / self.relative_auth)
        if data is not None and not isinstance(data, dict):
            raise ProviderError("Invalid Antigravity login format")
        return self.parse(data) if data is not None else None

    def write_active(self, login: Login) -> None:
        raw = json.dumps(login.data, separators=(",", ":")).encode("utf-8")
        if sys.platform == "win32":
            agy_keyring.windows_write(raw)
        elif sys.platform == "darwin":
            agy_keyring.mac_write(raw)
        else:
            write_json(Path.home() / self.relative_auth, login.data)

    def environment(self, profile: Path) -> dict[str, str]:
        env = child_environment()
        env.update(HOME=str(profile), USERPROFILE=str(profile), SSH_TTY="cswap",
                   AGY_CLI_DISABLE_AUTO_UPDATE="true", XDG_CONFIG_HOME=str(profile / ".config"),
                   XDG_DATA_HOME=str(profile / ".local/share"), APPDATA=str(profile / "AppData/Roaming"),
                   LOCALAPPDATA=str(profile / "AppData/Local"))
        return env


def parse_agy(data: dict) -> tuple:
    command = object_dict(data.get("command"))
    if data.get("status", "SUCCESS") != "SUCCESS" or command.get("name", "usage") != "usage":
        raise ProviderError("Antigravity usage command did not succeed")
    windows = []
    for group in objects(object_dict(command.get("data")).get("groups")):
        for bucket in objects(group.get("buckets")):
            fields = (group.get("display_name"), group.get("displayName"), group.get("name"),
                      bucket.get("name"), bucket.get("display_name"), bucket.get("displayName"),
                      bucket.get("id"), bucket.get("bucketId"), bucket.get("window"))
            description = " ".join(item.lower() for item in fields if isinstance(item, str))
            family = "Gemini" if "gemini" in description else "Claude and GPT" if "claude" in description or "gpt" in description else None
            if family is None:
                continue
            marker = str(bucket.get("window", "")).lower()
            if marker in ("weekly", "7d") or "7d" in description or "weekly" in description:
                period = "7d"
            elif marker == "5h" or "5h" in description or "5 hour" in description:
                period = "5h"
            else:
                continue
            fraction = bucket.get("remaining_fraction", bucket.get("remainingFraction"))
            if fraction is None:
                remaining = object_dict(bucket.get("remaining"))
                fraction = remaining.get("remainingFraction")
                if fraction is None and remaining.get("case") == "remainingFraction":
                    fraction = remaining.get("value")
            windows.append(window(f"{family} {period}", fraction, bucket.get("reset_time", bucket.get("resetTime")),
                                  scale=100, disabled=bucket.get("disabled") is True))
    if not any(item.remaining_pct is not None or item.disabled for item in windows):
        raise ProviderError("Antigravity returned no quota windows")
    return tuple(windows)


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, **process_options())
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.kill()


def _scoped_usage(executable: str, home: Path, env: dict[str, str]) -> dict:
    options = process_options() or {"start_new_session": True}
    with subprocess.Popen(
        [executable, "-p", "/usage", "--output-format", "json"], cwd=home, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options,
    ) as proc:
        auth = threading.Event()
        too_large = threading.Event()
        output = bytearray()

        def drain(stream, capture: bool) -> None:
            tail = b""
            for part in iter(lambda: stream.read1(4096), b""):
                if capture:
                    if len(output) + len(part) > MAX_OUTPUT:
                        too_large.set()
                    else:
                        output.extend(part)
                else:
                    combined = tail + part.lower()
                    if b"authentication required" in combined:
                        auth.set()
                    tail = combined[-64:]

        readers = [threading.Thread(target=drain, args=(proc.stdout, True), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, False), daemon=True)]
        for reader in readers:
            reader.start()
        end = time.monotonic() + USAGE_TIMEOUT
        try:
            while proc.poll() is None or any(reader.is_alive() for reader in readers):
                if auth.wait(0.05) or too_large.is_set() or time.monotonic() >= end:
                    if auth.is_set():
                        raise ProviderError("Authentication required. Sign in again with agy")
                    if too_large.is_set():
                        raise ProviderError("Antigravity usage response exceeded the size limit")
                    raise ProviderError("Antigravity usage command timed out")
        except BaseException:
            _kill_tree(proc)
            raise
        if auth.is_set():
            raise ProviderError("Authentication required. Sign in again with agy")
        if too_large.is_set():
            raise ProviderError("Antigravity usage response exceeded the size limit")
        if proc.returncode != 0:
            raise ProviderError("Antigravity usage command failed")
    try:
        data = json.loads(output)
    except ValueError:
        raise ProviderError("Antigravity returned invalid quota JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("Antigravity returned invalid quota JSON")
    return data


def agy_quota(store: AgyAccounts, account: Account) -> ProviderAccountQuota:
    row = ProviderAccountQuota("agy", account.label, account.number, account.active, True)
    with USAGE_SLOTS:
        try:
            available = agy_keyring.available_ram()
            if available is not None and available < MIN_RAM:
                return replace(row, status="skipped", error="Antigravity requires 3.5 GiB of available RAM")
            executable = shutil.which("agy")
            if not executable:
                raise ProviderError("agy CLI is not installed")
            with tempfile.TemporaryDirectory(prefix="cswap-agy-usage-") as tmp:
                home = Path(tmp)
                staged = home / store.relative_auth
                write_json(staged, account.login.data)
                result = _scoped_usage(executable, home, store.environment(home))
                if not account.active:
                    store.preserve_session_login(account, staged)
            return replace(row, windows=parse_agy(result))
        except ProviderError as exc:
            return replace(row, status="error", error=str(exc))
        except (OSError, ValueError):
            return replace(row, status="error", error="Unable to read Antigravity quota")
