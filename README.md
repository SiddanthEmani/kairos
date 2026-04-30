# kairos

Auto-curated AI events feed for the SF Bay Area, sourced from [Luma](https://lu.ma)'s
discover API. Filters and ranks them, drops anything that overlaps your day job,
publishes the survivors as a subscribable `.ics` calendar feed.

`kairos` (καιρός): the opportune moment.

## Two ways to run

### Cloud (GitHub Actions → `events.ics`)

Daily at 07:00 PT, GitHub Actions:

1. Pulls AI events from Luma (SF + San Jose + virtual, paginated 30-day window).
2. Filters: AI keyword in title or host calendar; weekday events must start ≥ 5 PM
   local; no past events.
3. Ranks by host-calendar reputation + technical title vocabulary, caps at top
   `max_events_per_run` (default 40).
4. Writes `events.ics` and commits it back to the repo.

Subscribe Apple Calendar / Google Calendar to:

```
https://raw.githubusercontent.com/<you>/kairos/main/events.ics
```

(In Calendar.app: `File → New Calendar Subscription`. In Google Calendar:
`Other calendars → + → From URL`.)

### Local (Apple Calendar via AppleScript)

`run.py --mode local` (default) inserts events directly into a named macOS
Calendar via parameterized AppleScript. Pair with launchd via `install.sh`.

```bash
./install.sh                 # one-time launchd setup
./cli.py status              # at-a-glance
./cli.py run-now --dry-run   # safe preview
./cli.py run-now             # for real
```

## Configuration (`config.json`)

| key | default | meaning |
|---|---|---|
| `cities` | `["san-francisco", "san-jose"]` | Luma `city_slug`s to query |
| `include_virtual_global` | `true` | also fetch virtual / global events |
| `lookahead_days` | `30` | drop events past this horizon |
| `min_weekday_hour_local` | `17` | weekday events must start at/after this hour, local TZ. `0` disables |
| `max_events_per_run` | `40` | top-N cap after ranking. `0` = unlimited |
| `calendar_name` | `"AI Events"` | local-mode target Calendar.app calendar |
| `run_time` | `"07:00"` | local-mode launchd schedule |

## Files

```
run.py                       # daily job (both modes)
cli.py                       # local TUI
config.json                  # user-editable
requirements.txt             # icalendar (cloud mode)
install.sh / uninstall.sh    # local launchd setup
.github/workflows/daily.yml  # cloud schedule
events.ics                   # cloud output (committed by CI)
```

## Why this exists

Luma's `category=ai` is a permissive tag — "Romanian IT in SF" and "Asian
American Voices" both surface in it. `kairos` adds:

- A keyword filter against title + host calendar name (LangChain, Snorkel,
  Modal, DeepMind, etc. carry signal that "Personal" calendars don't).
- Schedule awareness — if you're 9-5, weekday events at noon are noise.
- Rank-based capping so you see the top picks instead of every demo night.
