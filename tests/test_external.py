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
     "windows": [{"name": "Claude 5h", "used_pct": 20.0, "resets_at": "2099-01-01T08:00:00Z", "note": ""},
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
    def test_builtin_sources_without_a_file(self, root):
        sources = external.load_sources()
        assert [s.name for s in sources] == ["agy", "codex", "plans"]
        assert all(s.providers and not s.list_cmd for s in sources)
        assert [s.merge for s in sources] == [True, True, False]

    def test_file_replaces_disables_and_adds(self, root):
        (root / external.CONFIG_NAME).write_text(
            '[[source]]\nname = "plans"\nlist = ["z"]\nevery = 30\n\n'
            '[[source]]\nname = "codex"\nenabled = false\n\n'
            '[[source]]\nname = "mine"\nlist = ["x"]\nswitch = ["y", "{account}"]\nlabel = "me"\nmerge = true\n\n'
            '[[source]]\nname = "broken"\nlist = "not a list"\n', "utf-8")
        agy, plans, mine = external.load_sources()
        assert agy == external.BUILTIN[0]
        assert (plans.list_cmd, plans.providers, plans.every, plans.title) == (("z",), (), 30.0, "Coding plans")
        assert (mine.list_cmd, mine.switch_cmd, mine.label, mine.merge, mine.title) == (
            ("x",), ("y", "{account}"), "me", True, "mine")


class TestBuiltin:
    def test_rows_from_providers(self, root, monkeypatch):
        from claude_swap.providers import quota
        from claude_swap.providers.common import ProviderAccountQuota, QuotaWindow, ResetCredits

        rows = [
            ProviderAccountQuota("agy", "a@example.com", "1", True, True, windows=(
                QuotaWindow("Claude and GPT 5h", 80.0, "2099-01-01T08:00:00+00:00"),
                QuotaWindow("Gemini 7d", None, None, disabled=True))),
            ProviderAccountQuota("codex", "a@example.com", "1", False, True, plan="plus",
                                 windows=(QuotaWindow("5h", 40.0),),
                                 reset_credits=ResetCredits(available_count=2)),
            ProviderAccountQuota("kimi-coding", "CodexBar", windows=(QuotaWindow("7d", 90.0),)),
            ProviderAccountQuota("zai-glm", "-", status="unconfigured"),
        ]
        monkeypatch.setattr(quota, "collect_quota", lambda providers, refresh=False: [
            r for r in rows if r.provider in providers])
        agy, codex, plans = external.BUILTIN
        (a,) = external.fetch(agy)
        assert (a.account, a.active, a.switchable) == ("a@example.com", True, True)
        assert [(w.name, w.used_pct, w.note) for w in a.windows] == [("Claude 5h", 20.0, ""), ("Gemini 7d", None, "blocked")]
        assert a.windows[0].resets_at == "2099-01-01T08:00:00+00:00"
        (c,) = external.fetch(codex)
        assert (c.plan, c.note, [w.name for w in c.windows]) == ("plus", "2 reset credits", ["5h"])
        (k,) = external.fetch(plans)  # the unconfigured plan stays hidden
        assert (k.account, [w.name for w in k.windows], k.switchable) == ("kimi-coding", ["Kimi 7d"], False)

    def test_nothing_configured_hides_the_source(self, root, monkeypatch):
        from claude_swap.providers import quota
        from claude_swap.providers.common import ProviderAccountQuota

        monkeypatch.setattr(quota, "collect_quota", lambda providers, refresh=False: [
            ProviderAccountQuota(p, "-", status="unconfigured") for p in providers])
        assert external.fetch(external.BUILTIN[2]) == []

    def test_switch_uses_the_login_store(self, root, monkeypatch):
        from claude_swap.providers import quota

        calls = []

        class Store:
            def __init__(self, _root):
                pass

            def switch(self, identifier):
                calls.append(identifier)
                return identifier

        monkeypatch.setitem(quota.SWITCHERS, "codex", Store)
        monkeypatch.setattr(quota, "invalidate_quota", lambda provider, root=None: calls.append(f"invalidate {provider}"))
        assert external.switch(external.BUILTIN[1], "a@example.com") == (True, "Active codex account a@example.com")
        assert calls == ["a@example.com", "invalidate codex"]
        assert external.switch(external.BUILTIN[2], "kimi-coding") == (False, "plans is view-only")


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
        assert lines[0].startswith("    agy Claude 5h   ") and "20%" in lines[0]
        assert "active" not in text  # the header carries it
        assert lines[1].startswith("    agy Claude 7d   ") and "blocked" in lines[1]
        assert lines[0].index("━") == lines[1].index("─")  # bars start on the same column

    def test_reset_reads_like_a_claude_row(self, root):
        soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2 * 3600 + 120))
        acc = external.ExternalAccount("agy", "a@example.com", windows=(
            external.ExternalWindow("Claude 5h", 10.0, external._iso(soon)),))
        line = widgets.ext_rows(acc, "agy", 120, 14).plain.strip("\n")
        assert "resets 2h 1m" in line or "resets 2h 2m" in line
        assert " · " in line.split("resets", 1)[1]  # the clock follows, like "resets 2h 13m · 20:39"

    def test_card_header_and_full_window(self, root):
        acc = external.fetch(source(root))[1]
        card = widgets.external_card_text(acc, 120, prefix="agy", label_w=14)
        assert isinstance(card, Text)
        header, row = card.plain.split("\n")
        assert header.strip().startswith("b@example.com")
        assert row.startswith("    agy Gemini 7d ") and "100%" in row and "(!)" in row


class TestHeader:
    def test_product_segments_in_claude_order(self):
        acc = external.ExternalAccount("codex", "a@example.com", active=True, plan="plus",
                                       note="2 reset credits", product="Codex")
        assert widgets.ext_header_bits(acc, "Codex").plain == (
            "   ● Codex active   · Codex Plus   · Codex 2 reset credits")
        plan = external.ExternalAccount("plans", "zai-glm", plan="pro", product="GLM")
        assert widgets.ext_header_bits(plan, "").plain == "   · GLM Pro"

    def test_standalone_card_header(self):
        acc = external.ExternalAccount("agy", "b@example.com", active=True, product="agy",
                                       windows=(external.ExternalWindow("Claude 5h", 5.0),))
        header = widgets.external_card_text(acc, 120, prefix="agy").plain.split("\n")[0]
        assert header == "    b@example.com   ● agy active"


class TestCodex:
    def test_window_label_from_its_length(self):
        from claude_swap.providers.codex import window_label

        assert [window_label(s, "5h") for s in (18000, 604800, 2592000, 7200, 90, None, True)] == [
            "5h", "7d", "30d", "2h", "2m", "5h", "5h"]

    def test_free_plan_month_window_and_team_credit_pool(self):
        from claude_swap.providers.codex import parse_codex

        windows, plan = parse_codex({
            "plan_type": "free",
            "rate_limit": {"primary_window": {"used_percent": 30, "limit_window_seconds": 2592000,
                                              "reset_at": 4102444800}, "secondary_window": None},
        }, ())
        assert plan == "free"
        assert [(w.name, w.remaining_pct) for w in windows] == [("30d", 70.0)]
        windows, _ = parse_codex({
            "plan_type": "enterprise", "rate_limit": None,
            "spend_control": {"individual_limit": {"remaining_percent": 64, "resets_at": "2099-01-01T00:00:00Z"}},
        }, ())
        assert [(w.name, w.remaining_pct) for w in windows] == [("credits", 64.0)]

    def test_cloudflare_403_is_an_edge_block(self, monkeypatch):
        import io
        from urllib.error import HTTPError

        from claude_swap.providers import api
        from claude_swap.providers.common import ProviderError

        def opener(*_a, **_k):
            class O:
                def open(self, request, timeout):
                    raise HTTPError(request.full_url, 403, "Forbidden", {},
                                    io.BytesIO(b"<!DOCTYPE html><html><title>Just a moment...</title> cf-chl"))
            return O()

        monkeypatch.setattr(api, "build_opener", opener)
        with pytest.raises(ProviderError, match="edge"):
            api.get_usage("https://chatgpt.com/backend-api/wham/usage", {})
