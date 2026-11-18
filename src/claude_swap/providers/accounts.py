"""Back up the outgoing login before installing a selected profile."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.locking import FileLock

from .common import ProviderError, display_text, process_options, read_json, write_json


@dataclass(frozen=True)
class Login:
    identity: str = field(repr=False)
    label: str
    data: dict = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Account:
    number: str
    label: str
    login: Login = field(repr=False)
    active: bool = False
    path: Path | None = None


def _valid_label(label: str) -> bool:
    return (
        re.fullmatch(r"[A-Za-z0-9_+%@.-][A-Za-z0-9_+%@. -]{0,119}", label) is not None
        and label == label.strip(" .")
        and not label.isdecimal()
        and label.split(".", 1)[0].upper() not in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    )


class LocalAccounts:
    provider: str
    command: str
    relative_auth: Path

    def __init__(self, root: Path) -> None:
        self.directory = root / self.provider

    def parse(self, data: dict) -> Login:
        raise NotImplementedError

    def read_active(self) -> Login | None:
        raise NotImplementedError

    def write_active(self, login: Login) -> None:
        raise NotImplementedError

    def environment(self, profile: Path) -> dict[str, str]:
        raise NotImplementedError

    def stash_paths(self) -> list[Path]:
        return sorted(self.directory.glob("*.json"), key=lambda item: item.stem.casefold())

    def _current(self) -> tuple[Login | None, str | None]:
        try:
            return self.read_active(), None
        except ProviderError as exc:
            return None, str(exc)
        except (OSError, ValueError):
            return None, "Cannot read the active login"

    def roster(self) -> tuple[list[Account], str | None]:
        active, error = self._current()
        accounts = []
        seen: set[str] = set()
        invalid = False
        for path in self.stash_paths():
            try:
                data = read_json(path)
                if data is None:
                    continue
                login = self.parse(data)
            except (ProviderError, OSError, ValueError):
                invalid = True
                continue
            if login.identity in seen:
                continue
            seen.add(login.identity)
            is_active = active is not None and active.identity == login.identity
            label = path.stem if self.provider == "codex" else login.label
            label = display_text(label, *login.secrets, *(active.secrets if is_active else ())) or "account"
            accounts.append(Account(str(len(accounts) + 1), label, active if is_active else login, is_active, path))
        if active and active.identity not in seen:
            accounts.append(Account(str(len(accounts) + 1), active.label, active, True))
        return accounts, error or ("Some saved logins could not be read" if invalid else None)

    def _resolve(self, identifier: str) -> tuple[Account, list[Account]]:
        accounts, _ = self.roster()
        matches = [a for a in accounts if identifier == a.number or identifier.casefold() == a.label.casefold()]
        if len(matches) != 1:
            raise ProviderError("Account selector is ambiguous" if matches else "Account not found")
        return matches[0], accounts

    def _save(self, login: Login, accounts: list[Account], label: str | None = None) -> str:
        match = next((a for a in accounts if a.login.identity == login.identity and a.path), None)
        label = label or (match.label if match else login.label)
        if not _valid_label(label) or display_text(label, *login.secrets) != label:
            raise ProviderError("Use a short account name without credentials or special characters")
        if any(acc.path and acc.label.casefold() == label.casefold() and acc.login.identity != login.identity
               for acc in accounts):
            raise ProviderError("That account name belongs to another login. Save the current login with a unique name")
        path = self.directory / f"{label}.json"
        existing = read_json(path)
        if existing is not None and self.parse(existing).identity != login.identity:
            raise ProviderError("That account name belongs to another login")
        write_json(path, login.data)
        if match and match.path.parent == self.directory and match.path != path:
            if not match.path.samefile(path):
                match.path.unlink(missing_ok=True)
        return label

    def _lock(self) -> FileLock:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return FileLock(self.directory / ".lock")

    def add(self, label: str | None = None) -> str:
        with self._lock():
            active = self.read_active()
            if not active:
                raise ProviderError("No local login found. Sign in with the provider CLI first")
            return self._save(active, self.roster()[0], label)

    def switch(self, identifier: str) -> str:
        with self._lock():
            account, accounts = self._resolve(identifier)
            current = self.read_active()
            if current and current.identity == account.login.identity:
                return account.label
            if account.path is None:
                raise ProviderError("The active login changed. List accounts and try again")
            if current:
                self._save(current, accounts)
            if self.read_active() != current:
                raise ProviderError("The active login changed. List accounts and try again")
            self.write_active(account.login)
            return account.label

    def run(self, identifier: str, args: list[str]) -> int:
        account, _ = self._resolve(identifier)
        executable = shutil.which(self.command)
        if not executable:
            raise ProviderError(f"{self.command} CLI is not installed")
        with tempfile.TemporaryDirectory(prefix=f"cswap-{self.provider}-") as tmp:
            profile = Path(tmp)
            staged = profile / self.relative_auth
            write_json(staged, account.login.data)
            result = subprocess.run([executable, *args], env=self.environment(profile), **process_options())
            self.preserve_session_login(account, staged)
            return result.returncode

    def preserve_session_login(self, account: Account, staged: Path) -> None:
        """Save a refreshed inactive profile if no other writer moved it."""
        data = read_json(staged)
        if data is None or data == account.login.data:
            return
        updated = self.parse(data)
        if updated.identity != account.login.identity:
            return
        with self._lock():
            current = self.read_active()
            if current and current.identity == updated.identity:
                if self.provider == "codex" and current.data == account.login.data:
                    self.write_active(updated)
                return
            if account.path and read_json(account.path) == account.login.data:
                self._save(updated, [account])
