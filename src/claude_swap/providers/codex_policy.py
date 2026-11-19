"""Per-login Codex auto-switch policy, keyed by login identity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_swap.locking import FileLock

from .common import ProviderError, read_json, write_json


@dataclass(frozen=True)
class Policy:
    priority: int = 0
    held: bool = False


def _path(root: Path) -> Path:
    return root / "codex" / ".account-policy.json"


def decision_lock(root: Path) -> FileLock:
    return FileLock(root / "codex" / ".auto-state.lock")


def load_policies(root: Path) -> dict[str, Policy]:
    try:
        data = read_json(_path(root)) or {}
        policies = {}
        for identity, item in data.items():
            if not isinstance(item, dict):
                raise ValueError
            priority, held = item.get("priority", 0), item.get("held", False)
            if type(priority) is not int or type(held) is not bool:
                raise ValueError
            policies[identity] = Policy(priority, held)
        return policies
    except (OSError, ValueError):
        raise ProviderError("Cannot read Codex auto-switch policies") from None


def set_policy(root: Path, identity: str, *, priority: int | None = None,
               held: bool | None = None) -> Policy:
    path = _path(root)
    with decision_lock(root):
        policies = load_policies(root)
        previous = policies.get(identity, Policy())
        updated = Policy(previous.priority if priority is None else priority,
                         previous.held if held is None else held)
        if updated == Policy():
            policies.pop(identity, None)
        else:
            policies[identity] = updated
        write_json(path, {key: vars(value) for key, value in policies.items()})
        return updated
