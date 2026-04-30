#!/usr/bin/env python3
"""
AI Events — daily job.

Fetches AI events from Luma's discover JSON API (already pre-filtered
to category=ai), applies a 30-day lookahead and geo scope, dedupes
against seen-events.json, and inserts new events into Apple Calendar
via parameterized AppleScript.

Designed to be invoked by launchd on a schedule; also runnable manually
via cli.py or directly with --dry-run for testing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
STATE_PATH = PROJECT_DIR / "state.json"
SEEN_PATH = PROJECT_DIR / "seen-events.json"
LOGS_DIR = PROJECT_DIR / "logs"

USER_AGENT = "ai-events/1.0 (+local automation)"
HTTP_TIMEOUT = 30
LOG_RETENTION_DAYS = 90

DEFAULT_CONFIG = {
    "run_time": "07:00",
    "lookahead_days": 30,
    "cities": ["san-francisco", "san-jose"],
    "include_virtual_global": True,
    "calendar_name": "AI Events",
    # Don't surface events that overlap your day job. Weekday events whose
    # local-time start hour is below this number get dropped. Weekends are
    # always allowed regardless. 17 = 5 PM (right after a 9-5).
    "min_weekday_hour_local": 17,
    # IANA timezone for the schedule check. Required because cloud runners
    # (e.g. GitHub Actions) run in UTC; without this, "5 PM local" wrongly
    # means "5 PM UTC".
    "local_tz": "America/Los_Angeles",
    # Per-run insertion cap, applied AFTER ranking. 0 = no cap.
    "max_events_per_run": 40,
}

# --- Ranking signal vocab --------------------------------------------------
REPUTABLE_KW = (
    "langchain", "langgraph", "openai", "anthropic", "deepmind", "google",
    "snorkel", "hugging face", "huggingface", "modal", "databricks",
    "ai tinkerers", "ai engineer", "ai engineers", "south park commons",
    "y combinator", " yc ", "saastr", "veris ai", "gmi cloud", "mlops",
    "weaviate", "pinecone", "scale ai", "ai collective", "ai council",
    "ai salon", "resolve ai", "builders collective", "rosebud ai",
    "cursor", "vercel", "github", "nvidia", "perplexity", "microsoft",
    "frontier tower", "foresight institute", "novita ai",
)
TECH_KW = (
    "rag", "fine-tun", "fine tun", "agentic", "agent", "transformer",
    "diffusion", "embedding", "inference", "rlhf", "mcp", "llm",
    "evaluation", "benchmark", "alignment", "interpret", "hackathon",
    "workshop", "deep dive", "reading group", "research", "paper",
    "reinforcement", "multimodal", "robotics", "model", "prompt",
    "open source", "open-weight", "framework", "architecture", "training",
)
SOCIAL_KW = (
    "happy hour", "drinks", "dinner", "party", "mixer", "social",
    "afterparty", "after party", "brunch", "lunch", "racing party",
)

# Discover's `category=ai` is a loose tag — many generic tech/networking
# events leak through. Require an explicit AI-vocabulary hit in either the
# event title or the host calendar name. Calendar names like "LangChain
# Events" or "Snorkel AI" pass automatically; generic titles from generic
# calendars get dropped.
AI_PATTERN = re.compile(
    r"\bAI\b|\bAGI\b|\bA\.I\.\b|\bML\b|\bLLM[s]?\b|\bGPT[-\d]*\b"
    r"|\bRAG\b|\bMCP\b|\bRLHF\b|\bSLM\b|\bVLM\b"
    r"|\bClaude\b|\bChatGPT\b|\bGemini\b|\bLlama\b|\bMistral\b|\bGrok\b"
    r"|\bAnthropic\b|\bOpenAI\b|\bDeepMind\b|\bxAI\b|\bHugging\s?Face\b"
    r"|\bLangChain\b|\bLangGraph\b|\bLlamaIndex\b|\bPyTorch\b"
    r"|\bartificial intelligence\b|\bmachine learning\b|\bdeep learning\b"
    r"|\bgenerative\b|\bagentic\b|\bagent[s]?\b|\bcopilot[s]?\b"
    r"|\bneural\b|\btransformer[s]?\b|\bdiffusion\b"
    r"|\bembedding[s]?\b|\bfine[- ]?tun(?:e|ing)\b"
    r"|\bprompt(?:ing|s)?\b|\bfoundation model[s]?\b|\bmultimodal\b"
    r"|\balignment\b|\binference\b|\breinforcement learning\b"
    r"|\bvector (?:db|database|store|search)\b|\bopen[- ]?weight\b"
    r"|\bAI[- ](?:safety|alignment|agent|infra|infrastructure|hackathon)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = LOGS_DIR / f"{today}.log"

    logger = logging.getLogger("ai-events")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    return logger


def prune_old_logs() -> None:
    if not LOGS_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for p in LOGS_DIR.glob("*.log"):
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d") < cutoff:
                p.unlink()
        except (ValueError, OSError):
            continue


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in cfg:
            merged[k] = cfg[k]
    return merged


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(seen: dict) -> None:
    atomic_write_json(SEEN_PATH, seen)


def write_state(status: str, added: int, dup: int, filtered: int,
                errors: list[str]) -> None:
    payload = {
        "last_run": datetime.now().astimezone().isoformat(),
        "last_run_status": status,
        "events_added": added,
        "events_skipped_dup": dup,
        "events_skipped_filter": filtered,
        "errors": errors[:20],
    }
    atomic_write_json(STATE_PATH, payload)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def http_get(url: str, log: logging.Logger) -> bytes | None:
    # Luma silently degrades responses for non-browser clients. Use a real
    # browser UA and add Origin/Referer so the response is full data.
    # CORS-proxy services forward these headers to the target API, so the
    # combo works for direct calls and proxied calls alike.
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/html, */*",
        "Origin": "https://lu.ma",
        "Referer": "https://lu.ma/discover",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s on %s", e.code, _redact(url))
    except urllib.error.URLError as e:
        log.warning("URL error on %s: %s", _redact(url), e.reason)
    except Exception as e:  # noqa: BLE001
        log.warning("Fetch error on %s: %s", _redact(url), e)
    return None


def _redact(url: str) -> str:
    """Strip query strings before logging."""
    try:
        p = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:  # noqa: BLE001
        return "<url>"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class Event:
    __slots__ = ("event_id", "title", "start", "end", "location",
                 "description", "url", "is_virtual", "source",
                 "calendar_name")

    def __init__(self, event_id: str, title: str, start: datetime,
                 end: datetime, location: str, description: str, url: str,
                 is_virtual: bool, source: str, calendar_name: str = ""):
        self.event_id = event_id or ""
        self.title = (title or "(untitled)").strip() or "(untitled)"
        self.start = start
        self.end = end
        self.location = (location or "").strip()
        self.description = (description or "").strip()
        self.url = (url or "").strip()
        self.is_virtual = bool(is_virtual)
        self.source = source
        self.calendar_name = (calendar_name or "").strip()

    def dedup_key(self) -> str:
        return self.url or self.event_id


def _safe_str(v) -> str:
    return v if isinstance(v, str) else ""


# ---------------------------------------------------------------------------
# Discover JSON route (undocumented; defensive)
# ---------------------------------------------------------------------------

DISCOVER_URL = "https://api.lu.ma/discover/get-paginated-events"
DISCOVER_PAGE_LIMIT = 50
DISCOVER_MAX_PAGES = 20  # safety cap: 1000 events per source

# Free CORS-relay services. Used only when the direct discover call
# returns 0 entries (Luma silently empty-responds to many datacenter IPs,
# notably GitHub Actions runners). Local Macs hit api.lu.ma directly.
# These services don't require auth and proxy from non-datacenter IPs.
DISCOVER_PROXIES = (
    "https://api.allorigins.win/raw?url={}",
    "https://api.codetabs.com/v1/proxy/?quest={}",
)

# Map our city slugs -> Luma's HTML place slugs (for /discover/<slug>?category=ai).
# Many JSON `city_slug` values don't match a place page (e.g. "san-jose" has
# no Luma place page in 2026). Slugs not in the map fall through to the
# global discover/category/ai page.
CITY_HTML_SLUG = {
    "san-francisco": "sf",
    "new-york": "nyc",
    "los-angeles": "la",
    "seattle": "sea",
    "london": "london",
    "berlin": "berlin",
}
HTML_DISCOVER_GLOBAL = "https://lu.ma/discover/category/ai"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def _proxy_wrap(url: str, proxy_idx: int) -> str:
    """Wrap a target URL in a CORS-relay service URL."""
    enc = urllib.parse.quote(url, safe="")
    return DISCOVER_PROXIES[proxy_idx % len(DISCOVER_PROXIES)].format(enc)


def fetch_discover(city: str | None, lookahead_days: int,
                   log: logging.Logger) -> list[Event]:
    """Cursor-paginated discover fetch. Stops at the lookahead horizon
    (events come back in chronological order) or when has_more is False.
    If the direct call comes back blocked-empty (Luma silently returns
    {"entries":[]} from datacenter IPs), falls back to a CORS proxy and
    retries the whole pagination loop."""
    horizon = datetime.now(timezone.utc) + timedelta(days=lookahead_days)
    source = f"discover/{city or 'virtual'}"

    def _run(via_proxy: int | None) -> list[Event]:
        out: list[Event] = []
        cursor: str | None = None
        for page in range(DISCOVER_MAX_PAGES):
            params = {
                "category": "ai", "period": "future",
                "pagination_limit": str(DISCOVER_PAGE_LIMIT),
            }
            if city:
                params["city_slug"] = city
            if cursor:
                params["pagination_cursor"] = cursor
            target = f"{DISCOVER_URL}?{urllib.parse.urlencode(params)}"
            url = _proxy_wrap(target, via_proxy) if via_proxy is not None else target
            raw = http_get(url, log)
            if raw is None:
                log.info("  page=%d proxy=%s: HTTP failed", page, via_proxy)
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("  page=%d proxy=%s: non-JSON (%d bytes)",
                            page, via_proxy, len(raw))
                break
            page_events = _extract_discover_events(
                payload, source=source, log=log)
            has_more = bool(payload.get("has_more"))
            next_cursor = (payload.get("next_cursor")
                           if isinstance(payload, dict) else None)
            log.info("  page=%d proxy=%s: %d events has_more=%s cursor=%s",
                     page, via_proxy, len(page_events), has_more,
                     bool(next_cursor))
            out.extend(page_events)
            if page_events and page_events[-1].start > horizon:
                break
            if not has_more:
                break
            cursor = next_cursor
            if not cursor:
                break
        return out

    direct = _run(via_proxy=None)
    # When Luma blocks our IP it returns a tiny geo-personalized response
    # with has_more=False on page 0, so we get e.g. 3 photog events instead
    # of 500 SF AI events. A real first page returns ~50. Anything under
    # this floor is treated as blocked and retried through proxies. The
    # results are merged and de-duplicated by event id later in the
    # pipeline.
    BLOCK_FLOOR = 20
    if len(direct) >= BLOCK_FLOOR:
        return direct
    if direct:
        log.info("Direct discover for %s returned only %d — looks blocked,"
                 " trying proxies", source, len(direct))
    merged = list(direct)
    for idx in range(len(DISCOVER_PROXIES)):
        proxied = _run(via_proxy=idx)
        log.info("Proxy #%d for %s: %d events", idx, source, len(proxied))
        merged.extend(proxied)
    return merged


def _extract_discover_events(payload, source: str,
                             log: logging.Logger) -> list[Event]:
    """
    Defensive extractor. The discover endpoint is undocumented — accept a
    handful of plausible response shapes and skip anything we can't parse.
    """
    candidates = []
    if isinstance(payload, dict):
        for key in ("entries", "events", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                candidates = v
                break
    elif isinstance(payload, list):
        candidates = payload

    out: list[Event] = []
    for entry in candidates:
        try:
            ev = (entry.get("event")
                  if isinstance(entry, dict) and "event" in entry
                  else entry)
            if not isinstance(ev, dict):
                continue
            cal = entry.get("calendar") if isinstance(entry, dict) else None
            calendar_name = _safe_str(cal.get("name")) if isinstance(cal, dict) else ""
            api_id = _safe_str(ev.get("api_id") or ev.get("id"))
            title = _safe_str(ev.get("name") or ev.get("title"))
            url = _safe_str(ev.get("url"))
            if url and not url.startswith("http"):
                url = f"https://lu.ma/{url.lstrip('/')}"
            description = _safe_str(
                ev.get("description") or ev.get("description_short")
            )
            start_str = _safe_str(ev.get("start_at") or ev.get("starts_at"))
            end_str = _safe_str(ev.get("end_at") or ev.get("ends_at"))
            if not start_str:
                continue
            start = _parse_iso(start_str)
            end = _parse_iso(end_str) if end_str else start + timedelta(hours=1)
            geo = ev.get("geo_address_info") or ev.get("geo") or {}
            location = ""
            if isinstance(geo, dict):
                location = _safe_str(
                    geo.get("full_address") or geo.get("address")
                    or geo.get("city_state") or geo.get("city")
                )
            is_virtual = bool(ev.get("is_virtual") or ev.get("virtual")) \
                or not location
            out.append(Event(
                event_id=api_id or url,
                title=title, start=start, end=end,
                location=location, description=description, url=url,
                is_virtual=is_virtual, source=source,
                calendar_name=calendar_name,
            ))
        except Exception as e:  # noqa: BLE001
            log.debug("Skip malformed discover event in %s: %s", source, e)
    return out


def fetch_discover_html(city_slug: str | None,
                        log: logging.Logger) -> list[Event]:
    """Scrape lu.ma's server-rendered discover HTML for embedded events.
    Used because the JSON discover endpoint silently empty-responds (and
    serves geo-personalized junk) when called from cloud IPs. The HTML
    page works from anywhere because it's fetched as a regular browser
    page request.

    Returns up to ~30 events per city — Luma doesn't paginate the embed.
    Daily runs sweep the rolling window."""
    if city_slug:
        slug = CITY_HTML_SLUG.get(city_slug, city_slug)
        url = f"https://lu.ma/{slug}?category=ai"
    else:
        url = HTML_DISCOVER_GLOBAL
    raw = http_get(url, log)
    if not raw:
        return []
    src = raw.decode("utf-8", "replace")
    m = NEXT_DATA_RE.search(src)
    if not m:
        log.warning("No __NEXT_DATA__ in %s", _redact(url))
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("Malformed __NEXT_DATA__ in %s", _redact(url))
        return []
    pp = data.get("props", {}).get("pageProps", {})
    ini = pp.get("initialData", {}) or {}
    candidates: list = []
    if isinstance(ini, dict):
        # /sf?category=ai shape: initialData.data.{events,featured_events}
        d = ini.get("data") or {}
        if isinstance(d, dict):
            for k in ("featured_events", "events"):
                v = d.get(k)
                if isinstance(v, list):
                    candidates.extend(v)
        # /discover/category/ai shape: initialData.featured_place.events
        fp = ini.get("featured_place") or {}
        if isinstance(fp, dict):
            v = fp.get("events")
            if isinstance(v, list):
                candidates.extend(v)
    return _extract_discover_events(
        {"entries": candidates},
        source=f"html/{city_slug or 'discover'}",
        log=log,
    )


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
        if not m:
            raise
        dt = datetime.fromisoformat(m.group(1) + "+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def in_lookahead(event: Event, days: int) -> bool:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    return now <= event.start <= horizon


def is_ai_event(event: Event) -> bool:
    """Tighter than Luma's `category=ai`. Require AI vocabulary in the
    event title or in the host calendar name. Drops generic 'tech in
    Bay Area' events that the discover endpoint over-tags."""
    haystack = f"{event.title} | {event.calendar_name}"
    return bool(AI_PATTERN.search(haystack))


def fits_schedule(event: Event, min_weekday_hour: int,
                  tz_name: str = "America/Los_Angeles") -> bool:
    """True if a working-hours person could actually attend.
    - Weekends: always pass.
    - Weekdays: must start at or after `min_weekday_hour` in `tz_name`.
    `tz_name` is required because cloud runners are UTC by default.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = None
    s = event.start.astimezone(tz) if tz else event.start.astimezone()
    if s.weekday() >= 5:
        return True
    return s.hour >= min_weekday_hour


def event_score(event: Event) -> float:
    """Higher = better pick. Combines host calendar reputation + technical
    title vocabulary, with a small penalty for purely-social titles."""
    title = event.title.lower()
    cal = event.calendar_name.lower()
    score = 0.0
    if any(k in cal for k in REPUTABLE_KW):
        score += 4.0
    if any(k in title for k in REPUTABLE_KW):
        score += 2.0
    score += sum(1.2 for k in TECH_KW if k in title)
    score -= sum(0.4 for k in SOCIAL_KW if k in title)
    return score


def in_geo_scope(event: Event, cities: list[str],
                 include_virtual: bool) -> bool:
    if event.is_virtual and include_virtual:
        return True
    if not event.location:
        return include_virtual
    loc_lower = event.location.lower()
    needles = [slug.replace("-", " ") for slug in cities]
    return any(n in loc_lower for n in needles)


# ---------------------------------------------------------------------------
# AppleScript writer
# ---------------------------------------------------------------------------

# Read all event data from argv. No interpolation of untrusted input into
# the script source — equivalent of parameterized SQL.
APPLESCRIPT = r'''
on run argv
    if (count of argv) < 9 then
        return "ARGS_ERR"
    end if
    set theCalName to item 1 of argv
    set theTitle to item 2 of argv
    set startTuple to item 3 of argv
    set endTuple to item 4 of argv
    set theLocation to item 5 of argv
    set theDesc to item 6 of argv
    set theURL to item 7 of argv
    set isAllDay to item 8 of argv
    set theTZ to item 9 of argv

    set startDate to my buildDate(startTuple)
    set endDate to my buildDate(endTuple)

    tell application "Calendar"
        if not (exists calendar theCalName) then
            error "Calendar not found: " & theCalName
        end if
        tell calendar theCalName
            if isAllDay is "1" then
                make new event with properties {summary:theTitle, start date:startDate, end date:endDate, allday event:true, location:theLocation, description:theDesc, url:theURL}
            else
                make new event with properties {summary:theTitle, start date:startDate, end date:endDate, location:theLocation, description:theDesc, url:theURL}
            end if
        end tell
    end tell
    return "OK"
end run

on buildDate(s)
    set AppleScript's text item delimiters to "-"
    set parts to text items of s
    set AppleScript's text item delimiters to ""
    set y to (item 1 of parts) as integer
    set mo to (item 2 of parts) as integer
    set d to (item 3 of parts) as integer
    set hh to (item 4 of parts) as integer
    set mm to (item 5 of parts) as integer
    set dt to current date
    set year of dt to y
    set month of dt to mo
    set day of dt to d
    set hours of dt to hh
    set minutes of dt to mm
    set seconds of dt to 0
    return dt
end buildDate
'''.strip()


def insert_into_calendar(event: Event, calendar_name: str,
                         log: logging.Logger) -> bool:
    start_local = event.start.astimezone()
    end_local = event.end.astimezone()
    is_all_day = (
        start_local.hour == 0 and start_local.minute == 0
        and (end_local - start_local) >= timedelta(hours=23)
    )
    args = [
        "osascript", "-e", APPLESCRIPT,
        calendar_name,
        event.title[:200],
        f"{start_local.year}-{start_local.month}-{start_local.day}-"
        f"{start_local.hour}-{start_local.minute}",
        f"{end_local.year}-{end_local.month}-{end_local.day}-"
        f"{end_local.hour}-{end_local.minute}",
        event.location[:300],
        _build_description(event),
        event.url[:500],
        "1" if is_all_day else "0",
        str(start_local.tzinfo or ""),
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        log.error("osascript timeout for: %s", event.title)
        return False
    except FileNotFoundError:
        log.error("osascript not found — are you on macOS?")
        return False
    if result.returncode != 0 or "OK" not in (result.stdout or ""):
        # Cap stderr — may contain user-controlled strings
        msg = (result.stderr or "").strip().replace("\n", " ")[:200]
        log.error("osascript failed for %r: rc=%s stderr=%s",
                  event.title, result.returncode, msg)
        return False
    return True


def _build_description(event: Event) -> str:
    pieces: list[str] = []
    if event.url:
        pieces.append(event.url)
    if event.location:
        pieces.append(f"Location: {event.location}")
    if event.is_virtual:
        pieces.append("(Virtual event)")
    if event.calendar_name:
        pieces.append(f"Host: {event.calendar_name}")
    pieces.append(f"Source: {event.source}")
    if event.description:
        pieces.append("")
        pieces.append(event.description[:1500])
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# ICS feed writer (cloud mode)
# ---------------------------------------------------------------------------


def write_ics(events: list[Event], path: Path, log: logging.Logger,
              calname: str = "Kairos AI Events") -> int:
    """Render events as an iCalendar 2.0 feed. Apple Calendar / Google
    Calendar subscribe to the resulting file URL and refresh on their own
    schedule. Stateless: each run fully replaces the feed."""
    try:
        from icalendar import Calendar, Event as ICalEvent
    except ImportError:
        log.error("'icalendar' not installed. pip install icalendar")
        return 0

    cal = Calendar()
    cal.add("prodid", "-//kairos//ai-events//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calname)
    cal.add("x-wr-caldesc", "AI events in SF/SJ + virtual, curated by Kairos.")
    cal.add("x-published-ttl", "PT6H")  # hint clients to refresh every 6h

    for ev in events:
        item = ICalEvent()
        item.add("uid", (ev.event_id or ev.url or ev.title))
        item.add("summary", ev.title)
        item.add("dtstart", ev.start)
        item.add("dtend", ev.end)
        item.add("dtstamp", datetime.now(timezone.utc))
        if ev.url:
            item.add("url", ev.url)
        if ev.location:
            item.add("location", ev.location)
        desc = _build_description(ev)
        if desc:
            item.add("description", desc)
        cal.add_component(item)

    path.write_bytes(cal.to_ical())
    return len(events)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def fetch_all(config: dict, log: logging.Logger) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    errors: list[str] = []
    lookahead = config["lookahead_days"]

    for city in config.get("cities", []):
        log.info("Fetching %s", city)
        # HTML route works from any IP (used as primary on CI). The JSON
        # discover route gets richer pagination but silently empty-responds
        # from datacenter IPs, so we treat it as a bonus when available.
        h = fetch_discover_html(city, log)
        j = fetch_discover(city, lookahead, log)
        log.info("  %s: html=%d json=%d", city, len(h), len(j))
        events.extend(h); events.extend(j)

    if config.get("include_virtual_global"):
        log.info("Fetching virtual/global")
        h = fetch_discover_html(None, log)
        j = fetch_discover(None, lookahead, log)
        log.info("  virtual: html=%d json=%d", len(h), len(j))
        events.extend(h); events.extend(j)

    return events, errors


def _filter_pipeline(events: list[Event], config: dict,
                     log: logging.Logger,
                     seen: dict | None = None,
                     ) -> tuple[list[Event], int, int]:
    """Apply lookahead, AI relevance, geo scope, schedule, dedup, rank+cap.
    `seen` may be None (stateless) — then only intra-batch dedup is applied.
    Returns (kept_events_chrono_sorted, filtered_out, dup_skipped)."""
    filtered_out = 0
    dup_skipped = 0
    drops = {"lookahead": 0, "ai": 0, "geo": 0, "schedule": 0, "nokey": 0}
    kept: list[Event] = []
    intra: set[str] = set()
    min_wh = int(config.get("min_weekday_hour_local", 17))
    tz_name = str(config.get("local_tz", "America/Los_Angeles"))
    seen_keys = seen if isinstance(seen, dict) else {}

    for ev in events:
        if not in_lookahead(ev, config["lookahead_days"]):
            filtered_out += 1; drops["lookahead"] += 1; continue
        if not is_ai_event(ev):
            filtered_out += 1; drops["ai"] += 1; continue
        if not in_geo_scope(ev, config["cities"],
                            config["include_virtual_global"]):
            filtered_out += 1; drops["geo"] += 1; continue
        if not fits_schedule(ev, min_wh, tz_name):
            filtered_out += 1; drops["schedule"] += 1; continue
        key = ev.dedup_key()
        if not key:
            filtered_out += 1; drops["nokey"] += 1; continue
        if key in seen_keys or key in intra:
            dup_skipped += 1; continue
        intra.add(key)
        kept.append(ev)
    log.debug("Drop breakdown: %s", drops)

    cap = int(config.get("max_events_per_run", 0) or 0)
    if cap > 0 and len(kept) > cap:
        kept.sort(key=event_score, reverse=True)
        dropped = len(kept) - cap
        kept = kept[:cap]
        filtered_out += dropped
        log.info("Top-N cap: kept %d highest-ranked, dropped %d", cap, dropped)
    kept.sort(key=lambda e: e.start)
    return kept, filtered_out, dup_skipped


def run(dry_run: bool = False, mode: str = "local",
        ics_path: Path | None = None) -> int:
    log = setup_logging()
    prune_old_logs()
    config = load_config()
    log.info("=== Kairos run start (mode=%s dry_run=%s) ===", mode, dry_run)
    log.info(
        "Config: lookahead=%dd cities=%s virtual=%s cap=%d cal=%r",
        config["lookahead_days"], config["cities"],
        config["include_virtual_global"],
        int(config.get("max_events_per_run", 0) or 0),
        config["calendar_name"],
    )

    events, errors = fetch_all(config, log)
    log.info("Fetched %d raw events from all sources", len(events))

    if mode == "ics":
        # Stateless: each run fully rebuilds the feed.
        kept, filtered_out, _ = _filter_pipeline(events, config, log, seen=None)
        log.info("Filter: %d events kept, %d filtered out",
                 len(kept), filtered_out)
        out = ics_path or (PROJECT_DIR / "events.ics")
        n = write_ics(kept, out, log)
        log.info("Wrote %d events to %s", n, out)
        write_state(status="ok" if not errors else "partial",
                    added=n, dup=0, filtered=filtered_out, errors=errors)
        log.info("=== Run done: ics=%d errors=%d ===", n, len(errors))
        return 0 if not errors else 1

    # mode == "local": insert into Apple Calendar via AppleScript
    seen = load_seen()
    new_events, filtered_out, dup_skipped = _filter_pipeline(
        events, config, log, seen=seen,
    )
    log.info("Filter: %d new, %d duplicates, %d filtered out",
             len(new_events), dup_skipped, filtered_out)

    added = 0
    for ev in new_events:
        if dry_run:
            log.info("[dry-run] would add: %s @ %s",
                     ev.title, ev.start.isoformat())
            added += 1
            continue
        ok = insert_into_calendar(ev, config["calendar_name"], log)
        if ok:
            seen[ev.dedup_key()] = {
                "added": datetime.now().astimezone().isoformat(),
                "title": ev.title,
                "start": ev.start.isoformat(),
                "source": ev.source,
            }
            added += 1
            log.info("Added: %s @ %s", ev.title, ev.start.isoformat())
        else:
            errors.append(f"insert-failed:{ev.dedup_key()}")

    if not dry_run:
        save_seen(seen)
    write_state(
        status="ok" if not errors else "partial",
        added=added, dup=dup_skipped, filtered=filtered_out, errors=errors,
    )
    log.info("=== Run done: added=%d filtered=%d errors=%d ===",
             added, filtered_out, len(errors))
    return 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Kairos AI events daily job")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch+filter but do not write to Calendar")
    parser.add_argument("--mode", choices=("local", "ics"), default="local",
                        help="local: insert into Apple Calendar via AppleScript. "
                             "ics: write events.ics for subscription-style use.")
    parser.add_argument("--ics-path", default=None,
                        help="Output path for --mode ics (default events.ics)")
    args = parser.parse_args()
    ics_path = Path(args.ics_path) if args.ics_path else None
    try:
        return run(dry_run=args.dry_run, mode=args.mode, ics_path=ics_path)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        try:
            logging.getLogger("ai-events").exception("Unhandled: %s", e)
        except Exception:  # noqa: BLE001
            print(f"FATAL: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
