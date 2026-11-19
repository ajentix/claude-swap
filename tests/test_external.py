"""External account sources: config, the shared store, switching and card rows."""

from __future__ import annotations

import json
import sys
import time

import pytest
from rich.text import Text

from claude_swap import external
from claude_swap.tui import widgets

ROWS = [
    {"account": "a@example.com", "active": True, "plan": None, "note": "", "error": "",
     "windows": [{"name": "Claude 5h", "used_pct": 20.0, "resets": "17:00", "note": ""},
                 {"name": "Claude 7d", "used_pct": None, "resets": None, "note": "blocked"}]},
    {"account": "b@example.com", "active": False, "windows": [{"name": "Gemini 7d", "used_pct": 100}]},
]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(external, "get_backup_root", lambda: tmp_path)
    return tmp_path


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def source(tmp_path, rows=ROWS, *, every=60.0, switch=True, name="agy") -> external.Source:
    data = tmp_path / f"{name}.json"
    data.write_text(json.dumps(rows), "utf-8")
    counter = tmp_path / f"{name}.runs"
    list_code = (f"import pathlib,sys; c=pathlib.Path({str(counter)!r}); "
                 f"c.write_text(str(int(c.read_text() or 0)+1) if c.exists() else '1'); "
                 f"sys.stdout.write(pathlib.Path({str(data)!r}).read_text())")
    switch_cmd = py("import sys; print('switched to ' + sys.argv[1])") + ["{account}"] if switch else []
    return external.Source(name=name, list_cmd=tuple(py(list_code)), switch_cmd=tuple(switch_cmd),
                           every=every, timeout=30, title=name, label=name)


def runs(tmp_path, name="agy") -> int:
    path = tmp_path / f"{name}.runs"
    return int(path.read_text()) if path.exists() else 0


class TestConfig:
    def test_reads_sources_and_skips_invalid(self, root):
        (root / external.CONFIG_NAME).write_text(
            '[[source]]\nname = "agy"\nlist = ["x"]\nswitch = ["y", "{account}"]\n'
            'every = 300\ntitle = "Antigravity"\nlabel = "agy"\nmerge = true\n\n'
            '[[source]]\nname = "plans"\nlist = ["z"]\n\n'
            '[[source]]\nname = "broken"\nlist = "not a list"\n', "utf-8")
        agy, plans = external.load_sources()
        assert (agy.name, agy.list_cmd, agy.switch_cmd, agy.every) == ("agy", ("x",), ("y", "{account}"), 300.0)
        assert (agy.title, agy.label, agy.merge) == ("Antigravity", "agy", True)
        assert (plans.title, plans.label, plans.merge, plans.switch_cmd) == ("plans", "", False, ())

    def test_missing_file_means_no_sources(self, root):
        assert external.load_sources() == []


class TestStore:
    def test_runs_once_then_serves_the_store_while_fresh(self, root):
        src = source(root)
        first = external.fetch(src)
        assert [a.account for a in first] == ["a@example.com", "b@example.com"]
        assert first[0].active and first[0].switchable and first[0].fetched_at
        assert first[0].windows[1].used_pct is None and first[0].windows[1].note == "blocked"
        assert external.fetch(src) == first
        assert runs(root) == 1
        external.fetch(src, force=True)
        assert runs(root) == 2

    def test_due_after_the_interval(self, root):
        src = source(root, every=60)
        external.fetch(src)
        assert not external.due(src)
        assert external.due(src, now=time.time() + 61)

    def test_claimed_entry_is_not_run_again(self, root):
        src = source(root)
        external._update_store(src.name, claimed_at=time.time())
        assert external.fetch(src) == []
        assert runs(root) == 0

    def test_failed_run_keeps_last_rows_with_the_error(self, root):
        src = source(root)
        external.fetch(src)
        broken = external.Source(name=src.name, list_cmd=tuple(py("import sys; sys.exit('boom')")),
                                 every=src.every, timeout=30)
        rows = external.fetch(broken, force=True)
        assert [a.account for a in rows] == ["a@example.com", "b@example.com"]
        assert rows[0].error == "boom"

    def test_failed_first_run_is_one_error_row(self, root):
        broken = external.Source(name="x", list_cmd=tuple(py("print('not json')")), timeout=30)
        (row,) = external.fetch(broken)
        assert (row.account, row.error) == ("-", "list output is not JSON")

    def test_mark_active_moves_the_marker(self, root):
        src = source(root)
        external.fetch(src)
        external.mark_active(src, "b@example.com")
        assert [a.active for a in external.stored(src)] == [False, True]


class TestSwitch:
    def test_substitutes_the_account(self, root):
        ok, message = external.switch(source(root), "b@example.com")
        assert ok and message == "switched to b@example.com"

    def test_view_only_source(self, root):
        src = source(root, switch=False)
        assert external.switch(src, "a@example.com") == (False, "agy is view-only")
        assert not external.fetch(src)[0].switchable


class TestRows:
    def test_one_row_per_window_on_the_shared_column(self, root):
        acc = external.fetch(source(root))[0]
        assert widgets.ext_label_width(acc, "agy") == len("agy Claude 5h")
        text = widgets.ext_rows(acc, "agy", 120, 16).plain
        lines = text.strip("\n").split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("    agy Claude 5h   ") and "20%" in lines[0] and "agy active" in lines[0]
        assert lines[1].startswith("    agy Claude 7d   ") and "blocked" in lines[1]
        assert lines[0].index("━") == lines[1].index("─")  # bars start on the same column

    def test_card_header_and_full_window(self, root):
        acc = external.fetch(source(root))[1]
        card = widgets.external_card_text(acc, 120, prefix="agy", label_w=14)
        assert isinstance(card, Text)
        header, row = card.plain.split("\n")
        assert header.strip().startswith("b@example.com")
        assert row.startswith("    agy Gemini 7d ") and "100%" in row and "(!)" in row
