# Other accounts on the watch screen

`cswap watch` shows your other AI subscriptions beside your Claude accounts, a section per product, drawn the same way: each account's header names the product (`● Codex active · Codex Plus`), then one bar per rate-limit window with the same columns, the same `resets 2h 13m · 20:39` countdown and `(ahead of pace)` marker. `s` and Enter switch any of them.

```
Claude Code
 1  you@example.com   ● Claude active   · Claude Ultra
    Claude 7d      ━━━━━━━━━━━━━━━━━━━━──────────  64%  resets 18m · 13:00
    Claude Fable   ━━━━──────────────────────────  15%  resets 18m · 13:00

Antigravity
    you@example.com   ● agy active
    agy Claude 5h  ──────────────────────────────   0%  resets 4h 59m · 17:41
    agy Gemini 7d  ━━━━━━━━━━────────────────────  32%  resets 5d 19h · Nov 25 08:38  (ahead of pace)

Codex
    you@example.com   ● Codex active   · Codex Plus
    Codex 5h       ──────────────────────────────   0%  resets 4h 59m · 17:41
    Codex 7d       ──────────────────────────────   1%  resets 4d 3h · Nov 23 16:00

Coding plans
    kimi-coding
    Kimi 5h        ──────────────────────────────   1%  resets 4h 32m · 17:13
    Kimi 7d        ━━━━──────────────────────────  14%  resets 4d 22h · Nov 24 11:13
```

## Built in

Nothing to configure for what cswap already manages ([provider setup](provider-quota.md)):

- **Antigravity** and **Codex**: the active login plus every login saved with `cswap agy add` / `cswap codex add`. Save each account once while signed in to it; after that the watch screen switches between them.
- **Kimi Coding, z.ai, OpenCode Go**: any plan whose key cswap can find (environment variable, `providers.toml`, or an existing CodexBar, OpenCode or Kimi CLI login).

A provider with nothing set up stays hidden.

Some providers do not report a plan name (Antigravity's tier, Kimi's membership). Set it by hand and it shows in the header like any other:

```bash
cswap plan agy you@example.com Ultra     # one account
cswap plan agy '*' Pro                   # every account of that source
cswap plan plans kimi-coding Allegretto  # a coding plan
cswap plan agy you@example.com --unset
cswap plan                               # list them
```

A name set by hand wins over the provider's. They are kept in `external_plans.json` in the backup root.

## Your own sources

`external.toml` in the backup root (`~/.claude-swap-backup/` on Windows and macOS, `~/.local/share/claude-swap/` on Linux) can turn a built-in source off, replace it, or add one backed by your commands:

```toml
[[source]]
name = "codex"
enabled = false            # hide the built-in Codex rows

[[source]]
name = "plans"             # replace the built-in coding plans
title = "Coding plans"
list = ["python", "/path/to/plan_usage.py"]

[[source]]
name = "agy"
merge = true               # draw each login on the Claude card with the same email

[[source]]
name = "mine"
label = "mine"             # row prefix: "mine 5h"
list = ["python", "/path/to/usage.py"]
switch = ["python", "/path/to/switch.py", "{account}"]
every = 300                # seconds between runs of the list command (default 120)
timeout = 240              # a run that takes longer is abandoned (default 120)
```

The list command prints a JSON array, one object per account:

```json
[{"account": "you@example.com", "active": true, "plan": "pro", "note": "", "error": "",
  "windows": [{"name": "5h", "used_pct": 18.0, "resets_at": "2026-11-19T08:13:00Z", "note": ""},
              {"name": "7d", "used_pct": null, "resets_at": null, "note": "blocked"}]}]
```

`used_pct` is the share used, like the Claude bars; `null` draws an empty bar with the `note`. `resets_at` is an ISO time or epoch seconds. `error` shows under the account. Without `switch` the source is view-only.

## What to expect

- **Fast to open.** The last rows of every source are kept in `external_store.json` in the backup root. The watch screen paints them at once, with their age, and reads a source again only when its rows are older than `every` (Antigravity 5 minutes, since each reading starts the Antigravity CLI once per account; the others 2 minutes). Several `cswap watch` windows share one reading.
- **Failures keep the last rows.** A failed reading displays the error alongside the previous rows. The next automatic attempt follows the source interval. Editing a custom source's commands starts a new set of readings.
- **Switching.** Enter on an account switches it. On a merged card (`merge = true`) Enter asks which to switch: Claude Code, the other product, or all. After a switch the active marker moves at once; usage is not re-read. Antigravity applies a switch to new `agy` sessions; restart a running one. Codex can also switch on its own: `cswap codex auto`.
- **Your secrets stay where they are.** Built-in providers read the stores described in [provider setup](provider-quota.md); your own commands keep their keys to themselves, cswap only reads what they print.
