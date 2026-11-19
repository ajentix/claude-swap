"""Codex auto-switch: the decision rules, cooldown and dry-run."""

from __future__ import annotations

import time

import pytest

from claude_swap.providers import codex_auto
from claude_swap.providers.common import ProviderAccountQuota, QuotaWindow


def row(account, five, week, *, active=False, status="ok", disabled=False):
    windows = (QuotaWindow("5h", None if five is None else 100 - five, disabled=disabled),
               QuotaWindow("7d", None if week is None else 100 - week))
    return ProviderAccountQuota("codex", account, None, active, True, windows=windows, status=status)


class TestDecide:
    def test_stays_under_the_lines(self):
        d = codex_auto.decide([row("a", 50, 40, active=True), row("b", 0, 0)])
        assert (d.kind, d.exit_code) == ("no-switch", 2)

    def test_leaves_at_the_5h_line_for_the_most_room(self):
        d = codex_auto.decide([row("a", 92, 40, active=True), row("b", 50, 50), row("c", 10, 20)])
        assert (d.kind, d.source, d.target, d.exit_code) == ("switch", "a", "c", 0)

    def test_weekly_line_is_separate(self):
        rows = [row("a", 10, 93, active=True), row("b", 0, 0)]
        assert codex_auto.decide(rows, 90, 97).kind == "no-switch"
        assert codex_auto.decide(rows, 90, 92).target == "b"

    def test_margin_stops_ping_pong(self):
        d = codex_auto.decide([row("a", 91, 0, active=True), row("b", 89, 0)])
        assert d.kind == "blocked"  # b is under the line by 1 point and has barely more left (11% vs 9%)
        assert codex_auto.decide([row("a", 91, 0, active=True), row("b", 85, 0)]).target == "b"  # 15% vs 9%

    def test_all_over_moves_only_for_meaningfully_more_left(self):
        assert codex_auto.decide([row("a", 99, 0, active=True), row("b", 95, 0)]).target == "b"  # 5% vs 1%
        assert codex_auto.decide([row("a", 96, 0, active=True), row("b", 95, 0)]).kind == "blocked"

    def test_blocked_window_counts_as_at_limit(self):
        d = codex_auto.decide([row("a", None, 20, active=True, disabled=True), row("b", 0, 0)])
        assert d.target == "b"

    def test_unknown_active_usage_holds(self):
        d = codex_auto.decide([row("a", None, None, active=True, status="error"), row("b", 0, 0)])
        assert d.kind == "no-switch"

    def test_no_active_login_is_an_error(self):
        assert codex_auto.decide([row("b", 0, 0)]).exit_code == 1


class TestTick:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from claude_swap.providers import codex, quota

        rows = [row("a", 95, 0, active=True), row("b", 0, 0)]
        switched = []

        class Store:
            def __init__(self, _root):
                pass

            def switch(self, identifier):
                switched.append(identifier)
                return identifier

        monkeypatch.setattr(quota, "collect_quota", lambda providers, refresh=False, root=None: rows)
        monkeypatch.setattr(quota, "invalidate_quota", lambda provider, root=None: None)
        monkeypatch.setattr(codex, "CodexAccounts", Store)
        return tmp_path, switched

    def test_switches_then_cools_down(self, env):
        root, switched = env
        assert codex_auto.tick(root=root).kind == "switch"
        assert switched == ["b"] and codex_auto.last_switch(root) > time.time() - 5
        again = codex_auto.tick(root=root)
        assert again.kind == "no-switch" and "cooling down" in again.reason
        assert switched == ["b"]

    def test_dry_run_never_switches(self, env):
        root, switched = env
        d = codex_auto.tick(root=root, dry_run=True)
        assert d.kind == "no-switch" and d.target == "b" and switched == []
