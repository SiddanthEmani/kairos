# kyros

> **An iCalendar feed of the highest-signal AI events in the SF Bay Area,
> refreshed every 6 hours.** Ranked by host reputation, filtered for
> technical depth, skips events that clash with your work day. One URL —
> subscribe from any calendar app.

[![refresh](https://github.com/SiddanthEmani/kyros/actions/workflows/refresh.yml/badge.svg)](https://github.com/SiddanthEmani/kyros/actions/workflows/refresh.yml)
[![calendar feed](https://img.shields.io/badge/feed-events.ics-blue?logo=apple)](https://raw.githubusercontent.com/SiddanthEmani/kyros/main/events.ics)
[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![license](https://img.shields.io/github/license/SiddanthEmani/kyros)](LICENSE)

## Subscribe

```
https://raw.githubusercontent.com/SiddanthEmani/kyros/main/events.ics
```

- **Apple Calendar** &nbsp;`File → New Calendar Subscription` → paste URL
- **Google Calendar** &nbsp;`Other calendars → + → From URL` → paste URL
- **Outlook / Fantastical / anything else** &nbsp;Add subscription by URL

The feed refreshes four times a day. You'll see the next ~30 days of
hand-rankable AI talks, demos, hackathons, and salons — without the noise
of "AI for Founders Networking Drinks" or "Asian American Voices in Tech"
that Luma's `category=ai` lumps in.

`kyros` (κῦρος): authority, weight, signal over noise.

---

## How it works

```
Luma discover  ─►  filter  ─►  rank  ─►  cap  ─►  events.ics  ─►  your calendar
   ~1700/day      ~250 left    top 40     daily        you subscribe once
```

1. **Fetch** — paginates Luma's discover API across SF, San Jose, and
   global-virtual events for the next 30 days. Falls back to a CORS relay
   when the runner IP is blocked, so cloud and local pull the same data.
2. **Filter** — drops events past the horizon, events that don't mention
   AI vocabulary in the title or host calendar (catches the Luma `category=ai`
   leak), events outside SF/SJ, and weekday events that start before 5 PM.
3. **Rank** — scores by host reputation (LangChain, Snorkel, Modal,
   DeepMind, AI Tinkerers, …), technical title vocabulary (RAG, agentic,
   evals, fine-tuning, …), and lightly penalizes purely-social titles.
4. **Cap** — keeps the top N (default 40) and writes a clean `.ics`.

## Two ways to run

### Cloud (GitHub Actions → `events.ics`)

Fork the repo, enable Actions, done. The `kyros-refresh` workflow runs
every 6 hours (00, 06, 12, 18 UTC), regenerates `events.ics`, and commits
it back. Anyone subscribed to your raw URL gets the refresh automatically.

No secrets, no API keys, no servers — just a `.ics` file in a public repo.

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
.github/workflows/refresh.yml # cloud schedule
events.ics                   # cloud output (committed by CI)
```

## Why this exists

I wanted one calendar I could open on Sunday night and see every meaningful
AI talk in SF for the coming weeks — without scrolling through Luma's
"AI Founders Networking Drinks" or "Asian American Voices in Tech" that
its `category=ai` tag lumps in. The signal-to-noise on the raw discover
feed is brutal, and the events I actually want are spread across 50+
calendars.

`kyros` is the small cron that does that filtering for me, and the public
`.ics` URL means I can subscribe from any device and never think about it
again.

## License

MIT — fork it, run it for your city, ship your own feed.
