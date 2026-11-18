"""The provider quota table and its account switch actions."""

from __future__ import annotations

from datetime import datetime
from functools import partial

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from claude_swap.providers.common import KST, ProviderAccountQuota, ProviderError, home, quota_cells
from claude_swap.providers.quota import SWITCHERS, collect_quota, invalidate_quota


class QuotaScreen(Screen):
    BINDINGS = [
        Binding("r", "refresh_quota", "Refresh"),
        Binding("enter", "switch_selected", "Switch", priority=True),
        Binding("escape", "app.pop_screen", "Back"),
        *[Binding(str(i), f"switch_number({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self) -> None:
        super().__init__()
        self._table_accounts: list[ProviderAccountQuota] = []
        self._shortcuts: list[ProviderAccountQuota] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static("All Providers Quota", classes="quota-title")
        yield DataTable(id="quota-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="quota-status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Provider", "Account", "Plan", "Window", "Remaining", "Reset (KST)", "Status")
        table.focus()
        self._refresh(False)

    def _refresh(self, force: bool) -> None:
        if self._busy:
            return
        self._busy = True
        self.query_one("#quota-status", Static).update("Refreshing quotas...")
        self.run_worker(partial(self._collect, force), thread=True, exit_on_error=False)

    def _collect(self, force: bool) -> None:
        rows = collect_quota(refresh=force, switcher=self.app.switcher)
        self.app.call_from_thread(self._display, rows)

    def _display(self, rows: list[ProviderAccountQuota]) -> None:
        self._busy = False
        if not self.is_mounted:
            return
        table = self.query_one(DataTable)
        selection = table.cursor_row
        table.clear()
        self._table_accounts.clear()
        self._shortcuts = [row for row in rows if row.switchable and not row.active][:9]
        shortcuts = {(row.provider, row.number): str(i) for i, row in enumerate(self._shortcuts, 1)}
        now = datetime.now(KST)
        for row in rows:
            key = shortcuts.get((row.provider, row.number), "")
            label = f"{key} {row.account}" if key else row.account
            if row.active:
                label = f"* {label}"
            status = f"{row.status}: {row.error}" if row.error else ""
            for index, item in enumerate(row.windows or (None,)):
                remaining, reset = quota_cells(item, now) if item else ("-", "-")
                table.add_row(*map(Text, (
                    row.provider if index == 0 else "", label if index == 0 else "",
                    (row.plan or "-") if index == 0 else "", item.name if item else "-",
                    remaining, reset, status if index == 0 else "",
                )))
                self._table_accounts.append(row)
        if rows:
            table.move_cursor(row=min(selection, table.row_count - 1))
        self.query_one("#quota-status", Static).update("" if rows else "No provider accounts configured.")

    def action_refresh_quota(self) -> None:
        self._refresh(True)

    def action_switch_selected(self) -> None:
        cursor = self.query_one(DataTable).cursor_row
        if 0 <= cursor < len(self._table_accounts):
            self._switch(self._table_accounts[cursor])

    def action_switch_number(self, number: int) -> None:
        if number <= len(self._shortcuts):
            self._switch(self._shortcuts[number - 1])

    def _switch(self, row: ProviderAccountQuota) -> None:
        if self._busy or self.app.busy or not row.switchable or row.active or not row.number:
            return
        self._busy = True
        self.query_one("#quota-status", Static).update("Switching account...")
        self.run_worker(partial(self._switch_blocking, row), thread=True, exit_on_error=False)

    def _switch_blocking(self, row: ProviderAccountQuota) -> None:
        failure = None
        try:
            if row.provider == "claude":
                result = self.app.switcher.switch_to(row.number, json_output=True)
                if not result or not (result.get("switched") or result.get("reason") == "already-active"):
                    raise ProviderError("Claude did not activate the selected account")
            else:
                SWITCHERS[row.provider](home()).switch(row.account)
            invalidate_quota(row.provider)
        except ProviderError as exc:
            failure = str(exc)
        except Exception:
            failure = "Could not switch account"
        self.app.call_from_thread(self._switch_done, failure)

    def _switch_done(self, failure: str | None) -> None:
        self._busy = False
        if not self.is_mounted:
            return
        if failure:
            self.query_one("#quota-status", Static).update(failure)
        else:
            self._refresh(False)
