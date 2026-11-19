"""The provider quota table and its account switch actions."""

from __future__ import annotations

from datetime import datetime
from functools import partial

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Static

from claude_swap.providers.common import KST, ProviderAccountQuota, ProviderError, home, quota_cells, quota_status, reset_clock, reset_credit_cells
from claude_swap.providers.quota import SWITCHERS, collect_quota, invalidate_quota
from claude_swap.providers.reset import OUTCOMES, RESET_PROVIDERS, ResetIntent, ResetOptions, reset_options, redeem_reset
from claude_swap.tui.modals import ConfirmModal


class ResetCreditsScreen(ModalScreen[ResetIntent | None]):
    BINDINGS = [Binding("escape", "cancel", "Close")]
    DEFAULT_CSS = """
    ResetCreditsScreen { align: center middle; }
    ResetCreditsScreen > Vertical { width: 88%; height: 80%; padding: 1 2; border: solid $primary; background: $surface; }
    ResetCreditsScreen DataTable { height: 1fr; }
    ResetCreditsScreen Static { height: auto; }
    """

    def __init__(self, options: ResetOptions) -> None:
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Reset credits / {self.options.target.label}", markup=False)
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield Static("", id="reset-credit-status", markup=False)
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Credit", "Applies to", "Expires (KST)", "Left", "Status")
        now = datetime.now(KST)
        for index, offer in enumerate(self.options.offers, 1):
            table.add_row(*map(Text, (
                str(index), ", ".join(offer.windows) or "Unspecified", reset_clock(offer.expires_at, now),
                str(offer.remaining), "Available" if offer.usable else "Held",
            )), key=str(index))
        message = self.options.warning
        if self.options.pending_state:
            state = self.options.pending_state
            if self.options.pending and state == "unknown":
                scope = ", ".join(self.options.pending.offer.windows) or "-"
                table.add_row("Previous request", scope, "-", "-", "Retry same request", key="retry")
            message = message or ("Request is still in progress" if state == "sending" else "Outcome unknown")
        elif self.options.credits:
            message = f"{self.options.credits.available_count} credit(s) held"
            if self.options.credits.cooldown_until:
                message += f" / Cooldown until {reset_clock(self.options.credits.cooldown_until, now)} KST"
        self.query_one("#reset-credit-status", Static).update(message or "Balance unavailable")
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key == "retry":
            self.dismiss(self.options.pending)
        elif key:
            try:
                intent = self.options.choose(int(key))
            except ProviderError as exc:
                self.query_one("#reset-credit-status", Static).update(str(exc))
            else:
                self.dismiss(intent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class QuotaScreen(Screen):
    BINDINGS = [
        Binding("r", "refresh_quota", "Refresh"),
        Binding("x", "reset_credit", "Reset credits"),
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
        previous = self._table_accounts[selection] if 0 <= selection < len(self._table_accounts) else None
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
            status = quota_status(row, now)
            for index, item in enumerate(row.windows or (() if row.reset_credits else (None,))):
                remaining, reset = quota_cells(item, now) if item else ("-", "-")
                table.add_row(*map(Text, (
                    row.provider if index == 0 else "", label if index == 0 else "",
                    (row.plan or "-") if index == 0 else "", item.name if item else "-",
                    remaining, reset, status if index == 0 else "",
                )))
                self._table_accounts.append(row)
            if row.reset_credits:
                remaining, expiry, note = reset_credit_cells(row.reset_credits, now)
                table.add_row(*map(Text, (
                    "" if row.windows else row.provider, "" if row.windows else label, "",
                    "Reset credits", remaining, expiry, note if row.windows else " / ".join(filter(None, (status, note))),
                )))
                self._table_accounts.append(row)
        if rows:
            index = next((i for i, row in enumerate(self._table_accounts) if previous and
                          (row.provider, row.account, row.number) == (previous.provider, previous.account, previous.number)), None)
            table.move_cursor(row=index if index is not None else 0)
        self.query_one("#quota-status", Static).update("" if rows else "No provider accounts configured.")

    def action_refresh_quota(self) -> None:
        self._refresh(True)

    def action_reset_credit(self) -> None:
        cursor = self.query_one(DataTable).cursor_row
        if self._busy or self.app.busy or not 0 <= cursor < len(self._table_accounts):
            return
        row = self._table_accounts[cursor]
        if row.provider not in RESET_PROVIDERS or row.account == "-" or (row.provider != "zai-glm" and not row.number):
            self.notify("Reset credit redemption is unavailable for this account", severity="warning")
            return
        self._busy = True
        self.run_worker(partial(self._read_reset_offer, row), thread=True, exit_on_error=False)

    def _read_reset_offer(self, row: ProviderAccountQuota) -> None:
        try:
            identifier = row.account if row.provider == "zai-glm" else row.number
            options = reset_options(row.provider, identifier, switcher=self.app.switcher)
            account = options.target.label
            if row.account != account:
                raise ProviderError("The selected account changed. Refresh quotas first")
        except Exception as exc:
            error = str(exc) if isinstance(exc, ProviderError) else "Could not check reset credits"
            self.app.call_from_thread(self._reset_done, error, False)
        else:
            self.app.call_from_thread(self._show_reset_options, options)

    def _show_reset_options(self, options: ResetOptions) -> None:
        if self.is_mounted and self.app.screen is self:
            self.app.push_screen(ResetCreditsScreen(options), self._credit_selected)
        else:
            self._busy = False

    def _credit_selected(self, intent: ResetIntent | None) -> None:
        if intent is None:
            self._busy = False
        else:
            self._confirm_reset(intent)

    def _confirm_reset(self, intent: ResetIntent) -> None:
        if self.is_mounted and self.app.screen is self:
            self.app.push_screen(
                ConfirmModal(escape(intent.prompt()), title="Use reset credit", yes_label="Use credit", default_no=True),
                partial(self._on_reset_confirmed, intent),
            )
        else:
            self._busy = False

    def _on_reset_confirmed(self, intent: ResetIntent, confirmed: bool | None) -> None:
        if not confirmed:
            self._busy = False
            return
        self.run_worker(partial(self._redeem, intent), thread=True, exit_on_error=False)

    def _redeem(self, intent: ResetIntent) -> None:
        try:
            outcome = redeem_reset(intent, switcher=self.app.switcher)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ProviderError) else "Could not use reset credit"
            self.app.call_from_thread(self._reset_done, error, False)
        else:
            self.app.call_from_thread(self._reset_done, OUTCOMES[outcome], True)

    def _reset_done(self, message: str, refresh: bool) -> None:
        self._busy = False
        if self.is_mounted:
            self.notify(message, markup=False)
            if refresh:
                self._refresh(False)

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
