# Other accounts on the watch screen

`cswap watch` can show accounts from other tools next to your Claude accounts: an Antigravity login, a coding plan, anything whose usage a command can print. Each one is drawn like a Claude card (one bar per rate-limit window, on the same columns) and, if you give a switch command, `s` and Enter switch it too.

```
 1  you@example.com  [...]  ● active · Ultra
    Claude 7d      ━━━━━━━━━━━━━━──────────────────  62%  resets 32m · 13:00
    Claude Fable   ━━━─────────────────────────────  13%  resets 32m · 13:00
    agy Claude 5h  ────────────────────────────────   0%  resets 17:27
    agy Gemini 7d  ━━━━━━━━━━──────────────────────  32%  resets 11-25 08:38   ● agy active

    other@example.com
    agy Claude 7d  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%  resets 11-25 12:49  (!)

Coding plans
    kimi-coding
    Kimi 5h        ────────────────────────────────   1%  resets 17:13
    Kimi 7d        ━━━━━───────────────────────────  14%  resets 11-24 11:13
```

## Set it up

Put the sources in `external.toml` in the backup root (`~/.claude-swap-backup/` on Windows and macOS, `~/.local/share/claude-swap/` on Linux):

```toml
[[source]]
name = "agy"
label = "agy"            # row prefix: "agy Claude 5h"
merge = true             # same email as a Claude account: draw it on that card
list = ["python", "/path/to/agy_usage.py"]
switch = ["python", "/path/to/agy_switch.py", "{account}"]
every = 300              # seconds between runs of the list command (default 120)
timeout = 240            # a run that takes longer is abandoned (default 120)

[[source]]
name = "plans"
title = "Coding plans"   # section heading for accounts not merged into a Claude card
list = ["python", "/path/to/plan_usage.py"]
```

The list command prints a JSON array, one object per account:

```json
[{"account": "you@example.com", "active": true, "plan": "pro", "note": "", "error": "",
  "windows": [{"name": "Claude 5h", "used_pct": 18.0, "resets": "17:49", "note": ""},
              {"name": "Claude 7d", "used_pct": null, "resets": null, "note": "blocked"}]}]
```

`used_pct` is the share used, like the Claude bars; `null` draws an empty bar with the `note`. `error` shows under the account. Without `switch` the source is view-only.

## What to expect

- **Fast to open.** The last rows of every source are kept in `external_store.json` next to the config. The watch screen paints them at once, with their age, and runs a list command only when its rows are older than `every`. Several `cswap watch` windows share one run.
- **Failures keep the last rows.** A list command that fails or prints something that is not JSON leaves the previous rows on screen with the error added.
- **Switching.** Enter on an account from a source runs its switch command. On a Claude card that also carries a merged account, Enter asks which to switch: Claude Code, the source, or both. After a switch the active marker moves right away; usage is not re-read.
- **Your secrets stay in your commands.** cswap runs the commands and reads their output; it never sees the keys they use.
