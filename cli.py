#!/usr/bin/env python3
"""
AI Events — terminal UI.

Subcommand mode:
    ./cli.py status
    ./cli.py logs [--date YYYY-MM-DD] [--follow]
    ./cli.py run-now [--dry-run]
    ./cli.py config show
    ./cli.py config set-time HH:MM
    ./cli.py config set-cities sf,san-jose
    ./cli.py config set-virtual-global {on,off}
    ./cli.py config set-calendar "Calendar"

Menu mode (no subcommand): print a numbered menu, dispatch to the same
underlying functions.

The TUI never talks to Luma directly. It manages config and the launchd
plist, and shells out to run.py for manual runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
STATE_PATH = PROJECT_DIR / "state.json"
LOGS_DIR = PROJECT_DIR / "logs"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_LABEL = "com.siddanth.ai-events"
PLIST_INSTALLED = LAUNCH_AGENTS_DIR / f"{PLIST_LABEL}.plist"

DEFAULT_CONFIG = {
    "run_time": "07:00",
    "lookahead_days": 30,
    "cities": ["san-francisco", "san-jose"],
    "include_virtual_global": True,
    "calendar_name": "AI Events",
    "min_weekday_hour_local": 17,
    "max_events_per_run": 40,
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _green(s: str) -> str: return f"\033[32m{s}\033[0m"
def _red(s: str) -> str: return f"\033[31m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[33m{s}\033[0m"
def _bold(s: str) -> str: return f"\033[1m{s}\033[0m"
def _dim(s: str) -> str: return f"\033[2m{s}\033[0m"


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


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# launchd plist management
# ---------------------------------------------------------------------------

PLIST_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{run_py}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
    </dict>
    <key>WorkingDirectory</key><string>{workdir}</string>
    <key>StandardOutPath</key><string>{stdout}</string>
    <key>StandardErrorPath</key><string>{stderr}</string>
    <key>Nice</key><integer>10</integer>
    <key>LowPriorityIO</key><true/>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def _python_path() -> str:
    return sys.executable or "/usr/bin/python3"


def _gui_uid() -> str:
    return str(os.getuid())


def render_plist(run_time: str) -> str:
    hh, mm = run_time.split(":")
    return PLIST_BODY.format(
        label=PLIST_LABEL,
        python=_python_path(),
        run_py=str(PROJECT_DIR / "run.py"),
        workdir=str(PROJECT_DIR),
        stdout=str(LOGS_DIR / "launchd.out.log"),
        stderr=str(LOGS_DIR / "launchd.err.log"),
        hour=int(hh), minute=int(mm),
    )


def install_plist(run_time: str) -> tuple[bool, str]:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    PLIST_INSTALLED.write_text(render_plist(run_time))
    try:
        os.chmod(PLIST_INSTALLED, 0o644)
    except OSError:
        pass
    domain = f"gui/{_gui_uid()}"
    _run(["launchctl", "bootout", domain, str(PLIST_INSTALLED)])
    rc, out, err = _run(["launchctl", "bootstrap", domain,
                         str(PLIST_INSTALLED)])
    if rc != 0:
        return False, (err or out or "unknown").strip()
    return True, "ok"


def uninstall_plist() -> None:
    domain = f"gui/{_gui_uid()}"
    if PLIST_INSTALLED.exists():
        _run(["launchctl", "bootout", domain, str(PLIST_INSTALLED)])
        try:
            PLIST_INSTALLED.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """subprocess.run that returns (rc, stdout, stderr); rc=127 if not found."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"


# ---------------------------------------------------------------------------
# Commands — status, logs, run-now
# ---------------------------------------------------------------------------


def cmd_status() -> int:
    cfg = load_config()
    state = load_state()
    daemon_loaded = _is_daemon_loaded()

    print(_bold("AI Events"))
    print(f"  Daemon:           {_green('loaded') if daemon_loaded else _red('not loaded')}")
    print(f"  Daily run time:   {cfg['run_time']}")
    print(f"  Lookahead window: {cfg['lookahead_days']} days")
    print(f"  Cities:           {', '.join(cfg['cities']) or '(none)'}")
    print(f"  Virtual global:   {'on' if cfg['include_virtual_global'] else 'off'}")
    print(f"  Calendar:         {cfg['calendar_name']!r}")
    if state:
        last = state.get("last_run", "?")
        st = state.get("last_run_status", "?")
        col = _green if st == "ok" else _yellow if st == "partial" else _red
        print()
        print(_bold("Last run"))
        print(f"  When:    {last}")
        print(f"  Status:  {col(st)}")
        print(f"  Added:   {state.get('events_added', 0)}")
        print(f"  Dups:    {state.get('events_skipped_dup', 0)}")
        print(f"  Filtered:{state.get('events_skipped_filter', 0)}")
        errs = state.get("errors") or []
        if errs:
            print(f"  Errors ({len(errs)}):")
            for e in errs[:5]:
                print(f"    - {e}")
    else:
        print()
        print(_dim("No state yet — has the job run?"))
    return 0


def _is_daemon_loaded() -> bool:
    rc, _, _ = _run(["launchctl", "print",
                     f"gui/{_gui_uid()}/{PLIST_LABEL}"])
    return rc == 0


def cmd_logs(date: str | None, follow: bool) -> int:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    log_path = LOGS_DIR / f"{date}.log"
    if not log_path.exists():
        print(_yellow(f"No log for {date} at {log_path}"))
        return 1
    if not follow:
        sys.stdout.write(log_path.read_text())
        return 0
    # tail -f equivalent
    try:
        with log_path.open("r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def cmd_run_now(dry_run: bool) -> int:
    args = [_python_path(), str(PROJECT_DIR / "run.py")]
    if dry_run:
        args.append("--dry-run")
    print(_dim(f"$ {' '.join(args)}"))
    return subprocess.run(args).returncode


# ---------------------------------------------------------------------------
# Commands — config
# ---------------------------------------------------------------------------


def cmd_config_show() -> int:
    cfg = load_config()
    print(json.dumps(cfg, indent=2, sort_keys=True))
    return 0


def cmd_config_set_time(value: str) -> int:
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        print(_red("Time must be HH:MM (24-hour)"))
        return 1
    hh, mm = value.split(":")
    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        print(_red("Hours 0-23, minutes 0-59"))
        return 1
    cfg = load_config()
    cfg["run_time"] = f"{int(hh):02d}:{int(mm):02d}"
    save_config(cfg)
    if PLIST_INSTALLED.exists() or _is_daemon_loaded():
        ok, msg = install_plist(cfg["run_time"])
        if not ok:
            print(_yellow(f"Config saved, but launchd reload failed: {msg}"))
            return 2
    print(_green(f"Run time set to {cfg['run_time']} (daemon reloaded)"))
    return 0


def cmd_config_set_cities(value: str) -> int:
    parts = [c.strip().lower().replace(" ", "-") for c in value.split(",")
             if c.strip()]
    cfg = load_config()
    cfg["cities"] = parts
    save_config(cfg)
    print(_green(f"Cities set: {', '.join(parts) or '(none)'}"))
    return 0


def cmd_config_set_virtual_global(value: str) -> int:
    val = value.strip().lower()
    if val not in ("on", "off", "true", "false", "1", "0"):
        print(_red("Use on|off"))
        return 1
    cfg = load_config()
    cfg["include_virtual_global"] = val in ("on", "true", "1")
    save_config(cfg)
    print(_green(f"Virtual global: {'on' if cfg['include_virtual_global'] else 'off'}"))
    return 0


def cmd_config_set_calendar(value: str) -> int:
    cfg = load_config()
    cfg["calendar_name"] = value
    save_config(cfg)
    print(_green(f"Calendar: {value!r}"))
    return 0


# ---------------------------------------------------------------------------
# Menu mode
# ---------------------------------------------------------------------------


def menu() -> int:
    while True:
        cfg = load_config()
        state = load_state()
        loaded = _is_daemon_loaded()
        print()
        print(_bold("AI Events") + "  "
              + (_green("daemon loaded") if loaded else _red("daemon not loaded")))
        next_run = f"next run {cfg['run_time']}" if loaded else "(not scheduled)"
        last = state.get("last_run", "never") if state else "never"
        print(_dim(f"  {next_run}  ·  last run: {last}"))
        print()
        print("  [1] Status")
        print("  [2] View today's logs")
        print("  [3] Run now")
        print("  [4] Configure")
        print("  [q] Quit")
        try:
            choice = input("> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        if choice == "1":
            cmd_status()
        elif choice == "2":
            cmd_logs(date=None, follow=False)
        elif choice == "3":
            dry = input("dry-run? [y/N] ").strip().lower() == "y"
            cmd_run_now(dry_run=dry)
        elif choice == "4":
            _menu_configure()
        elif choice in ("q", "quit", "exit"):
            return 0
        else:
            print(_yellow("Unknown choice"))


def _menu_configure() -> None:
    while True:
        cfg = load_config()
        print()
        print(_bold("Configure"))
        print(f"  [1] Run time          ({cfg['run_time']})")
        print(f"  [2] Cities            ({', '.join(cfg['cities']) or '-'})")
        print(f"  [3] Virtual global    ({'on' if cfg['include_virtual_global'] else 'off'})")
        print(f"  [4] Calendar name     ({cfg['calendar_name']!r})")
        print(f"  [b] Back")
        try:
            choice = input("> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return
        if choice == "1":
            v = input("New run time HH:MM: ").strip()
            cmd_config_set_time(v)
        elif choice == "2":
            v = input("Cities (comma-separated slugs): ").strip()
            cmd_config_set_cities(v)
        elif choice == "3":
            v = input("Virtual global on/off: ").strip()
            cmd_config_set_virtual_global(v)
        elif choice == "4":
            v = input("Calendar name: ").strip()
            if v:
                cmd_config_set_calendar(v)
        elif choice == "b":
            return
        else:
            print(_yellow("Unknown choice"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="AI Events TUI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status")

    p_logs = sub.add_parser("logs")
    p_logs.add_argument("--date")
    p_logs.add_argument("--follow", "-f", action="store_true")

    p_run = sub.add_parser("run-now")
    p_run.add_argument("--dry-run", action="store_true")

    p_cfg = sub.add_parser("config")
    cfg_sub = p_cfg.add_subparsers(dest="cfg_cmd")
    cfg_sub.add_parser("show")
    p_t = cfg_sub.add_parser("set-time"); p_t.add_argument("value")
    p_c = cfg_sub.add_parser("set-cities"); p_c.add_argument("value")
    p_v = cfg_sub.add_parser("set-virtual-global"); p_v.add_argument("value")
    p_n = cfg_sub.add_parser("set-calendar"); p_n.add_argument("value")

    args = parser.parse_args()

    if args.cmd is None:
        return menu()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "logs":
        return cmd_logs(date=args.date, follow=args.follow)
    if args.cmd == "run-now":
        return cmd_run_now(dry_run=args.dry_run)
    if args.cmd == "config":
        if args.cfg_cmd in (None, "show"):
            return cmd_config_show()
        if args.cfg_cmd == "set-time":
            return cmd_config_set_time(args.value)
        if args.cfg_cmd == "set-cities":
            return cmd_config_set_cities(args.value)
        if args.cfg_cmd == "set-virtual-global":
            return cmd_config_set_virtual_global(args.value)
        if args.cfg_cmd == "set-calendar":
            return cmd_config_set_calendar(args.value)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
