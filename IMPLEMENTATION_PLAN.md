# AI Events — Implementation Plan

**Goal:** every day, fetch AI events from Luma and insert them into Apple Calendar (your primary calendar). Backend only, no UI. The only thing you ever look at is Calendar.app.

---

## 1. Reality check on the Luma API

I researched the docs before designing. Two important findings:

**(a) The official Luma API is host-focused, not discovery-focused.** Endpoints like `GET https://public-api.luma.com/v1/calendar/list-events` and `https://api.lu.ma/public/v1/event/get` return events *managed by your calendar* — i.e., events you host. Auth is `x-luma-api-key`, and using the API requires a **Luma Plus subscription** on the calendar. There is no documented endpoint for "find me public AI events near me."

**(b) The Discover feed runs on Luma's undocumented internal API** (`api.lu.ma/...`). Luma's own help docs explicitly say these can change without notice. Public reads (event listings on discover/city/tag pages) don't require auth, but they aren't a contract — anything we build on them needs to fail gracefully.

**Practical implication:** the only durable backend route to public AI events on Luma is a thin wrapper around their internal JSON endpoints, with a fallback strategy for when they shift.

---

## 2. Three data routes

### 2.1 Your personal subscribed feed (primary signal)
When you're logged into Luma, `lu.ma/calendar` is your aggregated feed of every calendar you've subscribed to / follow. Luma exposes a personal iCal subscription URL for it (looks like `https://api.lu.ma/ics/get?entity=usr-...&token=...`). The token in that URL is an auth secret — anyone holding it sees your subscribed events. We:
- Pull this URL once during setup via the TUI.
- Store it in **macOS Keychain** under a service name like `ai-events.luma.subscribed-ics`. Never in a file, never in env vars, never in logs.
- Daily run reads it from Keychain via `security find-generic-password`, fetches the `.ics`, parses with `icalendar`.

This route gives you everything from organizations you've actively subscribed to — the highest-quality signal.

### 2.2 Luma discover JSON (geo + virtual)
Endpoints used by lu.ma's own frontend (e.g. `api.lu.ma/discover/get-paginated-events`) with geo and category filters. Returns JSON. No auth needed for public reads. Three queries per day:
- **San Francisco** in-person events.
- **San Jose / South Bay** in-person events. (Slug TBD at step 1 — Luma's geo taxonomy isn't documented; we'll inspect the actual response.)
- **Virtual / global**, AI-tagged.

Treated as untrusted: defensive parsing, schema validation, log-and-skip on malformed records. The internal API can change without notice — failure is non-fatal, the script just logs and continues with the other routes.

### 2.3 Curated public .ics list (resilience + coverage)
A handful of well-known AI Luma calendars (e.g. "AI Tinkerers SF") expose public `.ics` URLs. We keep a list in `ai-luma-calendars.txt` — adding/removing is a one-line edit. Stable, official, no auth. Catches events the discover feed misses and provides a fallback if the internal JSON API breaks.

**Combination logic:** all three routes feed into the same normalize → filter → dedup → write pipeline. Dedup is keyed on event URL, so the same event appearing in two routes is added once.

---

## 3. Architecture — fully local, no cloud

```
                  ┌──────────────────────────────────┐
                  │  cli.py  (terminal UI for setup, │
                  │   monitoring, logs, manual run)  │
                  └─────────────────┬────────────────┘
                                    │ writes config
                                    ▼
                          config.json + Keychain
                                    │ reads config
                                    ▼
launchd (daily at user-configured time)
                  │
                  ▼
~/.../AI Events/run.py  ──  fetches Luma, dedups, classifies
        │
        ├── reads:  config.json              (run time, feature flags)
        │           ai-luma-calendars.txt    (public .ics URLs)
        │           seen-events.json         (dedup state)
        │           Keychain                 (personal subscribed-feed URL)
        ├── writes: seen-events.json         (updated)
        │           logs/YYYY-MM-DD.log      (one log per run)
        │           state.json               (last-run timestamp, counts)
        ▼
osascript subprocess  ──  inserts events into Calendar.app
        │
        ▼
Apple Calendar  ("Calendar" — your primary, events first-class & editable)
```

Everything runs on your Mac. No remote scheduler, no cloud DB, no MCP at runtime.

**Why local launchd instead of the scheduled-tasks MCP:**
- Runs even if you're not using Claude that day.
- No data leaves your Mac.
- No dependency on any remote service being up.
- launchd is the standard macOS daemon mechanism — same thing the OS uses for its own jobs.

---

## 4. Components

### 4.1 `run.py` — the daily job
A single Python script. Stdlib where possible (`urllib`, `json`, `subprocess`, `pathlib`, `datetime`); one third-party dep — `icalendar` — for parsing `.ics`. No async, no frameworks. Easy to read top-to-bottom in <350 lines.

Pipeline inside the script:
1. Load `config.json` and `seen-events.json` (create if missing).
2. Fetch personal subscribed `.ics` (token from Keychain) → parse.
3. Fetch Luma JSON discover endpoint for SF, San Jose, and global-virtual.
4. Fetch each `.ics` URL from `ai-luma-calendars.txt`.
5. Normalize all three sources into a uniform `Event` shape.
6. Drop events outside the 30-day lookahead window.
7. Pre-filter on AI keywords (cheap regex pass; configurable strictness).
8. Drop events already in `seen-events.json`.
9. For each new event, hand to AppleScript writer (see §5).
10. Atomically write `seen-events.json` and `state.json` (last-run + counts).
11. Append a structured log line per event + a summary line to `logs/YYYY-MM-DD.log`.

### 4.2 `cli.py` — terminal UI
Small interactive TUI. Stdlib only (`argparse` + plain prompts; no `curses`/`textual` — keep it simple). Two ways to invoke:

**Subcommand mode** (scriptable):
```
./cli.py status              # last run, next run, # events added today
./cli.py logs                # tail today's log (or --date YYYY-MM-DD)
./cli.py logs --follow       # tail -f equivalent
./cli.py run-now             # trigger a manual run
./cli.py config              # interactive config editor
./cli.py config set-time 7:00
./cli.py config set-cities sf,san-jose
./cli.py config set-luma-token   # prompts; stores in Keychain
./cli.py feeds list          # public .ics calendars
./cli.py feeds add <url>
./cli.py feeds remove <url>
```

**Menu mode** (no subcommand): prints a numbered menu —
```
AI Events — daemon active, next run 2026-04-30 07:00
[1] Status        [2] View today's logs    [3] Run now
[4] Configure     [5] Manage public feeds  [6] Quit
>
```

Both modes hit the same underlying functions. The menu is just a thin wrapper for casual use; the subcommands are what you'd use from a script or alias.

The TUI never talks to Luma directly — it only reads/writes config and shells out to `run.py` for manual runs. Keeps responsibilities clean.

### 4.3 `config.json`
```json
{
  "run_time": "07:00",
  "lookahead_days": 30,
  "cities": ["san-francisco", "san-jose"],
  "include_virtual_global": true,
  "filter_strictness": "balanced",
  "calendar_name": "Calendar"
}
```
Edited via TUI, but it's just a JSON file — you can hand-edit if you want. Never contains secrets. The TUI re-installs the launchd plist whenever `run_time` changes.

### 4.4 `state.json`
Read-only-ish file the daily run writes after each invocation:
```json
{
  "last_run": "2026-04-29T07:00:14-07:00",
  "last_run_status": "ok",
  "events_added": 3,
  "events_skipped_dup": 12,
  "events_skipped_filter": 47
}
```
Used by `cli.py status` so the TUI doesn't need to parse logs.

### 4.5 `com.siddanth.ai-events.plist` — launchd job
LaunchAgent (runs as you, not root) that fires `run.py` daily at the time in `config.json`. Loaded via `launchctl bootstrap`. No special privileges. The plist is regenerated by `cli.py` from a template when run-time changes, then `launchctl bootout` + `bootstrap` to reload.

### 4.6 `ai-luma-calendars.txt`
Plain newline-delimited list of public `.ics` URLs. Edited via `cli.py feeds add/remove` or by hand.

### 4.7 `seen-events.json`
```json
{
  "<luma-event-url>": { "added": "2026-04-29T07:00:12-07:00", "title": "..." }
}
```
Atomic writes only. If corrupted, the script logs and treats it as empty — so a bad write at worst causes one day of duplicates, never a crash loop.

### 4.8 `logs/YYYY-MM-DD.log`
One log per day. Format: ISO timestamp, level, message. Rotated by date naturally — no log4j-style config needed. Auto-pruned after 90 days by a tiny housekeeping pass at the start of `run.py`.

---

## 5. AppleScript event insertion (Option B)

Per event, the script generates AppleScript like:

```applescript
tell application "Calendar"
  tell calendar "Calendar"  -- your primary calendar's name
    make new event with properties {¬
      summary: "<title>", ¬
      start date: date "<formatted>", ¬
      end date: date "<formatted>", ¬
      location: "<location or empty>", ¬
      description: "<luma url + host + description>", ¬
      url: "<luma url>"¬
    }
  end tell
end tell
```

Run via `osascript -e <script>` as a subprocess. Calendar doesn't even need to be open. The event goes into your primary calendar and is fully editable like any other event.

**One thing to confirm at setup:** the exact name of your primary calendar in Calendar.app. We hardcode it in the script after first run.

---

## 6. Security

You asked for this explicitly, so here's the threat model and what we do.

**Threats considered:**
- *Secrets exfiltration — your personal Luma feed URL.* This URL contains an auth token and is the most sensitive thing in the system. Stored only in **macOS Keychain** (`security add-generic-password -s ai-events.luma.subscribed-ics -a $USER -w <url>`). Never written to disk in plaintext, never logged, never appears in `config.json`. The `cli.py config set-luma-token` flow reads it via `getpass` (no echo) and writes straight to Keychain. If Keychain is locked at run time, `run.py` skips the subscribed-feed route and logs a warning — it does not block the rest of the run.
- *Untrusted input from Luma → AppleScript injection.* Event titles/descriptions come from the open internet. They will contain quotes, backslashes, newlines — and could be deliberately malicious. We never string-format AppleScript with raw input. Two layers:
  1. Use AppleScript's `do shell script` style only when unavoidable; prefer property-list passing.
  2. Pass event data via **stdin** to `osascript`, with the script reading from `system attribute` or argv, not concatenated into the source. Equivalent of parameterized SQL.
- *Malicious URLs in event descriptions.* We never auto-open URLs. The URL field in Calendar is just a string Calendar displays — clicking is your action, not the script's.
- *Outbound exfil from a compromised dep.* Single third-party dep (`icalendar`), pinned by hash via `pip install --require-hashes`. Stdlib for everything else.
- *Privilege escalation.* launchd runs as you, not root. The script never asks for sudo. File permissions on the project folder are user-only (700/600).
- *Replay / re-run causing duplicate calendar pollution.* Dedup is keyed on the stable Luma URL; running the script 5x on the same day is a no-op after the first run.
- *Network egress blocked.* Script handles 4xx/5xx and timeouts gracefully — never wedges, just logs and exits.
- *Calendar.app compromise.* AppleScript can only do what your user can do. We scope to one calendar by name; we never touch other calendars or run shell commands from inside the AppleScript.

**Hardening checklist (applied):**
- `chmod 700` on project dir, `chmod 600` on `seen-events.json` and logs.
- All HTTP requests have explicit timeouts (10s connect, 30s read).
- All external input is type-checked before use.
- Dedup file is atomically written (temp file + `os.replace`).
- launchd plist sets `Nice=10`, `LowPriorityIO=true`, no `RunAtLoad`.
- Logs never contain API keys, full event descriptions are truncated to 500 chars.

---

## 7. Files this project will produce

```
AI Events/
├── IMPLEMENTATION_PLAN.md               (this file)
├── run.py                               (the daily job, ~350 lines)
├── cli.py                               (terminal UI, ~250 lines)
├── config.json                          (user-editable config, no secrets)
├── state.json                           (last-run summary; written by run.py)
├── ai-luma-calendars.txt                (public .ics URLs)
├── seen-events.json                     (dedup state)
├── com.siddanth.ai-events.plist         (launchd config; symlinked into ~/Library/LaunchAgents/)
├── install.sh                           (one-time setup: bootstraps launchd, sets perms)
├── uninstall.sh                         (removes launchd job + Keychain entry; leaves data)
└── logs/
    └── YYYY-MM-DD.log                   (one per run)
```

Ten files, none of them long. The whole system is deliberately small.

---

## 8. Build order

1. **Probe the Luma JSON endpoint.** Hit `api.lu.ma/discover/...` for SF and San Jose, capture the actual response shape (city slugs, fields, pagination), build the parser around what's real — not what the docs imply.
2. **Probe the personal subscribed feed.** I'll walk you through grabbing your `lu.ma/calendar` iCal URL once, store it in Keychain, and verify we can fetch + parse it.
3. **Build `run.py` skeleton** — all three fetch routes, normalized output, no calendar writes yet. Run it manually, inspect the merged event list, tune the AI keyword filter against real data.
4. **Dedup store + atomic writes.**
5. **AppleScript insertion** — first against a throwaway test calendar, then "Calendar" once we're confident escaping is bulletproof for messy titles.
6. **`cli.py`** — status, logs, config, run-now, feeds. Build subcommand mode first; menu mode is a thin wrapper on top.
7. **launchd plist + install.sh** — bootstrap, verify it fires at the configured time, confirm an event lands in Calendar.
8. **End-to-end dry run** — let it fire once on its own, check Calendar, check logs.
9. **Run for a week**, watch the logs via `./cli.py logs`, prune the keyword filter and the geo slugs.

Setup after step 7 is a single `./install.sh` followed by `./cli.py config set-luma-token` to paste your subscribed-feed URL. After that, nothing to do day-to-day — `cli.py` is there when you want to peek.

---

## 9. Confirmed parameters

| | |
|---|---|
| Sources | personal subscribed feed + discover JSON (SF + San Jose + global virtual) + curated public .ics |
| Lookahead | 30 days |
| Calendar target | "Calendar" (your primary), via AppleScript |
| Run schedule | configurable via TUI; default 7:00 AM |
| TUI | `cli.py` — subcommands + simple menu, stdlib only |
| Filter strictness | configurable, default "balanced" |

No outstanding questions blocking step 1. Ready to start building when you say go.

---

## Sources

- [Luma API · Luma Help](https://help.luma.com/p/luma-api)
- [Getting Started with the Luma API](https://docs.luma.com/reference/getting-started-with-your-api)
- [List Events endpoint](https://docs.luma.com/reference/get_v1-calendar-list-events)
- [Get Event endpoint](https://docs.lu.ma/reference/get_public-v1-event-get)
- [Luma API on Arcade Docs](https://docs.arcade.dev/en/resources/integrations/productivity/luma-api)
- [Luma on Nango Docs](https://nango.dev/docs/integrations/all/luma)
