#!/usr/bin/env python3
"""Keepalive nudges for coding-agent terminals (Claude Code, Codex CLI, Grok Build).

A background job from the OS scheduler (launchd on macOS, a systemd user timer
or cron on Linux, Task Scheduler on Windows) runs `nudge.py fire` every minute;
it types a nudge message into the terminal of each opted-in agent TUI whose
own interval has elapsed. Sessions are off by default; `enable` (alias `on`)
opts the current one in by its TUI PID, so an opt-in ends with its process and
never sticks to a terminal that a later session reuses.

usage: nudge.py [status|enable|on|disable|off] [--every MINUTES] [--pid N]
       nudge.py once --at HH:MM [--pid N]
       nudge.py install [--interval DEFAULT_MINUTES] [--elevated] | uninstall
       nudge.py test [--channel tmux|wezterm|iterm2|console] [--pid N]
       nudge.py fire                      (what the scheduler runs each minute)

status, enable, install and test first make sure the scheduler job exists
and points at this script, creating it from templates/ if not (disable never
installs anything). Until a
`test` has passed on this machine, those commands also print first-run setup
questions ("ASK-USER [...]") for the agent to put to the user.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SYSTEM = platform.system()  # Darwin | Linux | Windows
WINDOWS = SYSTEM == "Windows"
SCRIPT = Path(__file__).resolve()
TEMPLATES = SCRIPT.parent.parent / "templates"
NAME = "session-nudge"
LAUNCHD_LABEL = "local.session-nudge"
CRON_MARK = "# session-nudge"

if WINDOWS:
    STATE = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / NAME
    )
else:
    STATE = (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / NAME
    )
STATE = Path(os.environ.get("SESSION_NUDGE_HOME") or STATE)
INCLUDE = (
    STATE / "include"
)  # "PID every=MIN last=EPOCH tty=... session=..." per opted-in TUI
CONFIG = (
    STATE / "config.json"
)  # {"interval_minutes": 61 (default pace), "message": "...", ...}
LOG = STATE / "nudge.log"
TEST_REQUEST = STATE / "test-request"  # one-shot: the next fire nudges only these PIDs
VERIFIED = STATE / "verified"  # written when an end-to-end test passes
LOCK = STATE / "lock"  # serializes fire runs and edits of the opt-in list

DEFAULT_INTERVAL = 61
DEFAULT_MESSAGE = (
    "[{now}] Automated nudge: continue working unless blocked on a user decision or all work "
    "is complete. Ignore any retry-after, usage-reset-on, etc. notices in context - usage may "
    "have reset earlier than stated; try resuming immediately. [next nudge at {next}]"
)
TEST_MARKER = "session-nudge-test-target"
AGENT_RE = re.compile(
    r"(^|[\s/\\\"])(claude|codex|grok)(-tui)?(\.exe)?(\"|\s|$)|@anthropic-ai[/\\]claude-code|"
    + TEST_MARKER,
    re.I,
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if WINDOWS:  # under pythonw every console child would flash a window
        kw.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, **kw)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:  # keep the newer half
        LOG.write_text(LOG.read_text(errors="replace")[-500_000:])
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("interval_minutes", DEFAULT_INTERVAL)
    cfg.setdefault("message", DEFAULT_MESSAGE)
    cfg.setdefault(
        "elevated", False
    )  # Windows: register the task with highest privileges
    return cfg


def save_config(**changes) -> None:
    cfg = config()
    cfg.update(changes)
    STATE.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


# --- processes ---------------------------------------------------------------


def proc(pid: int) -> dict | None:
    """{'ppid', 'tty', 'command'} of a live process, or None. tty is None without one."""
    if WINDOWS:
        out = run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
                "Select-Object ParentProcessId,Name,CommandLine | ConvertTo-Json",
            ]
        )
        try:
            p = json.loads(out.stdout)
        except ValueError:
            return None
        # No tty notion on Windows; delivery attaches to the process's console.
        return {
            "ppid": p.get("ParentProcessId") or 0,
            "tty": "console",
            "command": p.get("CommandLine") or p.get("Name") or "",
        }
    out = run(["ps", "-o", "ppid=,tty=,command=", "-ww", "-p", str(pid)])
    parts = out.stdout.strip().split(None, 2)
    if out.returncode or len(parts) < 3:
        return None
    ppid, tty, command = parts
    # macOS prints "??" and Linux "?" for no controlling terminal.
    tty = None if "?" in tty else (tty if tty.startswith("/dev/") else "/dev/" + tty)
    return {"ppid": int(ppid), "tty": tty, "command": command}


def is_agent_tui(pid: int) -> dict | None:
    p = proc(pid)
    return p if p and p["tty"] and AGENT_RE.search(p["command"]) else None


def claude_sessions() -> list[dict]:
    out = []
    for f in (Path.home() / ".claude" / "sessions").glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
    return out


def process_cwd(pid: int) -> str | None:
    """Read a process cwd on platforms that expose it."""
    if SYSTEM == "Darwin":
        result = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        if result.returncode:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return os.path.realpath(line[1:])
    elif SYSTEM == "Linux":
        try:
            return os.path.realpath(f"/proc/{pid}/cwd")
        except OSError:
            return None
    return None


def agent_tuis_in_cwd(cwd: str) -> list[tuple[int, str]]:
    """Find agent TUI processes in cwd when called from an API worker."""
    if WINDOWS:
        return []
    result = run(["ps", "-axo", "pid=,tty=,command=", "-ww"])
    candidates = []
    if result.returncode:
        return candidates
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_text, tty_text, command = parts
        if not pid_text.isdigit() or "?" in tty_text or not AGENT_RE.search(command):
            continue
        pid = int(pid_text)
        process = proc(pid)
        if process and process["tty"] and process_cwd(pid) == cwd:
            candidates.append((pid, process["tty"]))
    return sorted(candidates)


def find_target(explicit: int | None) -> tuple[int, str] | None:
    """(pid, tty) of the agent TUI whose terminal shows this session; None after printing why."""
    if explicit:
        p = proc(explicit)
        if not p or not p["tty"]:
            print(f"pid {explicit} is not running in a terminal; no change made.")
            return None
        return explicit, p["tty"]

    # Claude Code names its TUI in CLAUDE_PID.
    me = int(os.environ.get("CLAUDE_PID") or 0)
    sessions = {int(s.get("pid", 0)): s for s in claude_sessions()}
    if me and sessions.get(me, {}).get("kind", "interactive") == "interactive":
        p = is_agent_tui(me)
        if p:
            return me, p["tty"]

    # Otherwise the nearest agent ancestor with a terminal (Codex, or Claude
    # Code without CLAUDE_PID).
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        p = proc(pid)
        if not p:
            break
        if p["tty"] and AGENT_RE.search(p["command"]):
            return pid, p["tty"]
        pid = p["ppid"]

    # A Claude Code background session is shown through an interactive TUI
    # attached to it, which the session files do not name: take the single
    # one in the same working directory.
    if me in sessions:
        cwd = sessions[me].get("cwd") or os.getcwd()
        tuis = sorted(
            (pid, p["tty"])
            for pid, s in sessions.items()
            if s.get("kind") == "interactive" and s.get("cwd") == cwd
            for p in [is_agent_tui(pid)]
            if p
        )
        if len(tuis) == 1:
            return tuis[0]
        if tuis:
            print(
                "Several claude terminals could be showing this session; rerun with --pid:"
            )
            for pid, tty in tuis:
                print(f"  --pid {pid}  ({tty}, {sessions[pid].get('name', '')})")
            return None
        print(
            f"No interactive claude terminal in {cwd} shows this session, so a nudge "
            "could not reach it anyway; no change made."
        )
        return None

    # API/tool workers are not necessarily children of the visible agent TUI.
    # Match by project cwd instead, and refuse to guess if multiple terminals fit.
    cwd = os.path.realpath(os.getcwd())
    tuis = agent_tuis_in_cwd(cwd)
    if len(tuis) == 1:
        return tuis[0]
    if tuis:
        print(f"Several agent terminals are open in {cwd}; rerun with --pid:")
        for pid, tty in tuis:
            print(f"  --pid {pid}  ({tty})")
        return None
    print(f"Not inside an agent terminal and no agent TUI was found in {cwd}; no change made.")
    return None


# --- opt-in list ----------------------------------------------------------------


def listed(path: Path | None = None) -> dict[int, dict[str, str]]:
    """PID -> its key=value fields; other words on a line are ignored."""
    path = INCLUDE if path is None else path
    entries = {}
    if path.exists():
        for line in path.read_text().splitlines():
            words = line.split()
            if words and words[0].isdigit():
                entries[int(words[0])] = dict(
                    w.split("=", 1) for w in words[1:] if "=" in w
                )
    return entries


def write_listed(entries: dict[int, dict[str, str]]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    live = {p: f for p, f in entries.items() if proc(p)}  # drop ended sessions
    tmp = INCLUDE.with_suffix(".tmp")
    tmp.write_text(
        "".join(
            " ".join([str(p)] + [f"{k}={v}" for k, v in f.items()]) + "\n"
            for p, f in sorted(live.items())
        )
    )
    os.replace(tmp, INCLUDE)


def every(fields: dict[str, str]) -> int:
    try:
        return max(1, int(fields.get("every", "")))
    except ValueError:
        return int(config()["interval_minutes"])


def next_local_time(at: str, now: float | None = None) -> int:
    """Epoch for the next local HH:MM, or tomorrow if today's time passed."""
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("time must be in 24-hour HH:MM form")
    hour, minute = map(int, at.split(":"))
    now = time.time() if now is None else now
    local = time.localtime(now)
    candidate = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                             hour, minute, 0, -1, -1, -1))
    if candidate <= now:
        tomorrow = datetime.fromtimestamp(now).date() + timedelta(days=1)
        candidate = time.mktime((tomorrow.year, tomorrow.month, tomorrow.day,
                                 hour, minute, 0, -1, -1, -1))
    return int(candidate)


def schedule_once(pid: int, tty: str, at: int) -> None:
    """Replace this TUI's recurring opt-in with one scheduled delivery."""
    with state_lock() as locked:
        if not locked:
            raise RuntimeError("The nudge state is busy; try again in a minute.")
        entries = listed()
        entries[pid] = {"once_at": str(at), "tty": tty}
        if not WINDOWS:
            for other in [q for q, fields in entries.items()
                          if q != pid and fields.get("tty") == tty]:
                del entries[other]
        write_listed(entries)


# --- delivery ---------------------------------------------------------------------
# Every channel types the message, waits 0.5s, sends CR, waits 1s and sends a
# second CR. Only CR submits; a CR in the same burst as the text reads as part
# of a paste in Claude Code and the message sits unsent in the composer. The
# second CR catches a swallowed first one and does nothing on an empty
# composer. The same keystrokes submit in the Codex TUI.


def deliver_tmux(targets: dict[str, int], message: str) -> set[str]:
    sent = set()
    if not shutil.which("tmux"):
        return sent
    out = run(["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{pane_id}"])
    for line in out.stdout.splitlines():
        tty, _, pane = line.partition(" ")
        if tty in targets and tty not in sent:
            if (
                run(["tmux", "send-keys", "-t", pane, "-l", "--", message]).returncode
                == 0
            ):
                time.sleep(0.5)
                run(["tmux", "send-keys", "-t", pane, "Enter"])
                time.sleep(1)
                run(["tmux", "send-keys", "-t", pane, "Enter"])
                sent.add(tty)
                log(f"tmux {pane} ({tty}): sent")
    return sent


def deliver_wezterm(targets: dict[str, int], message: str) -> set[str]:
    sent = set()
    if not shutil.which("wezterm"):
        return sent
    out = run(["wezterm", "cli", "list", "--format", "json"])
    try:
        panes = json.loads(out.stdout or "[]")
    except ValueError:
        return sent
    for pane in panes:
        tty, pid = pane.get("tty_name"), str(pane.get("pane_id"))
        if tty in targets and tty not in sent:
            send = ["wezterm", "cli", "send-text", "--pane-id", pid, "--no-paste"]
            if run(send + [message]).returncode == 0:
                time.sleep(0.5)
                run(send + ["\r"])
                time.sleep(1)
                run(send + ["\r"])
                sent.add(tty)
                log(f"wezterm pane {pid} ({tty}): sent")
    return sent


def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def deliver_iterm2(targets: dict[str, int], message: str) -> set[str]:
    if (
        SYSTEM != "Darwin"
        or "iTerm.app/Contents/MacOS/iTerm2"
        not in run(["ps", "axww", "-o", "command="]).stdout
    ):
        return set()
    ttys = ", ".join(applescript_str(t) for t in targets)
    script = f"""
with timeout of 60 seconds
  tell application "iTerm2"
    set ok to {{}}
    -- By window id: "item N of windows" follows z-order, which shifts as
    -- windows open, close or come to the front. try: a window or session
    -- closing mid-loop must not abort the others.
    repeat with wid in (get id of every window)
      try
        repeat with t in tabs of window id wid
          repeat with s in sessions of t
            try
              set tt to tty of s as text
              if tt is in {{{ttys}}} then
                tell s to write text {applescript_str(message)} newline NO
                delay 0.5
                tell s to write text (ASCII character 13) newline NO
                copy tt to end of ok
                delay 1
                tell s to write text (ASCII character 13) newline NO
              end if
            end try
          end repeat
        end repeat
      end try
    end repeat
    set {{tid, AppleScript's text item delimiters}} to {{AppleScript's text item delimiters, linefeed}}
    set res to ok as text
    set AppleScript's text item delimiters to tid
    return res
  end tell
end timeout"""
    out = run(["osascript", "-"], input=script)
    if out.returncode:
        log(f"iterm2: osascript failed: {out.stderr.strip()}")
    sent = {t for t in out.stdout.split("\n") if t in targets}
    for t in sorted(sent):
        log(f"iterm2 {t}: sent")
    return sent


def deliver_console(pid: int, message: str) -> bool:
    """Windows: write key events into the console input buffer of pid."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT)]

    k32.CreateFileW.restype = wintypes.HANDLE

    def keys(text: str, vk: int = 0) -> ctypes.Array:
        recs = (INPUT_RECORD * (2 * len(text)))()
        for i, ch in enumerate(text):
            for j, down in enumerate((True, False)):
                r = recs[2 * i + j]
                r.EventType = 1  # KEY_EVENT
                r.Event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, 0, ch, 0)
        return recs

    def write(handle, recs) -> bool:
        n = wintypes.DWORD()
        return bool(k32.WriteConsoleInputW(handle, recs, len(recs), ctypes.byref(n)))

    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        log(f"console pid {pid}: AttachConsole failed ({ctypes.get_last_error()})")
        return False
    try:
        h = k32.CreateFileW(
            "CONIN$", 0xC0000000, 3, None, 3, 0, None
        )  # RW, share RW, OPEN_EXISTING
        if h in (None, wintypes.HANDLE(-1).value):
            log(f"console pid {pid}: cannot open CONIN$ ({ctypes.get_last_error()})")
            return False
        ok = write(h, keys(message))
        time.sleep(0.5)
        ok = write(h, keys("\r", 0x0D)) and ok
        time.sleep(1)
        write(h, keys("\r", 0x0D))
        k32.CloseHandle(h)
    finally:
        k32.FreeConsole()
    log(f"console pid {pid}: {'sent' if ok else 'write failed'}")
    return ok


@contextmanager
def state_lock(wait: float = 50):
    """Exclusive lock on the state directory; yields False if not acquired in time.

    Without it two overlapping ticks (cron does not wait for the previous run,
    and a test fire can coincide with a tick) would both see a session as due
    and nudge it twice, and an `on` racing a tick could lose its edit.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "a+")
    deadline = time.time() + wait
    while True:
        try:
            if WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                f.close()
                yield False
                return
            time.sleep(0.5)
    try:
        yield True
    finally:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


def fire() -> None:
    """One scheduler tick, under the state lock so overlapping ticks cannot double-nudge."""
    with state_lock() as locked:
        if locked:
            fire_locked()
        else:
            log("previous run still holds the lock -- tick skipped")


def fire_locked() -> None:
    """Nudge each opted-in session whose interval has elapsed."""
    cfg = config()
    now = time.time()
    if TEST_REQUEST.exists():
        sessions = listed(TEST_REQUEST)
        TEST_REQUEST.unlink()
        due = list(sessions)
        due_fields = {pid: sessions[pid].copy() for pid in due}
        log(f"--- test fire for pid {', '.join(map(str, due))}")
    else:
        sessions = listed()
        due, due_fields, changed = [], {}, False
        for pid, f in list(sessions.items()):
            if "once_at" in f:
                try:
                    at = float(f["once_at"])
                except ValueError:
                    log(f"pid {pid}: invalid one-time schedule -- removed")
                    del sessions[pid]
                    changed = True
                    continue
                if now >= at:
                    due.append(pid)
                    due_fields[pid] = f.copy()
                continue
            try:
                last = float(f["last"])
            except (KeyError, ValueError):  # no record yet: start its clock now
                f["last"], changed = str(int(now)), True
                continue
            if now - last >= every(f) * 60 - 30:  # ticks are ~60s apart
                due.append(pid)
                due_fields[pid] = f.copy()
        if not due:
            if changed:
                write_listed(sessions)
            return
        for pid in due:
            if "once_at" in sessions[pid]:
                del sessions[pid]
            else:
                sessions[pid]["last"] = str(int(now))
        write_listed(sessions)
        log(f"--- firing for pid {', '.join(map(str, due))}")

    # Sessions with the same pace share one message (its "next" time). A
    # terminal is nudged at most once per tick, even if several opted-in PIDs
    # (or paces) point at it.
    groups: dict[tuple[int, bool], dict[str, int]] = {}
    seen: set[str] = set()
    for pid in sorted(due, key=lambda q: ("once_at" in due_fields[q], every(due_fields[q]))):
        p = is_agent_tui(pid)
        if p:
            key = p["tty"] if not WINDOWS else str(pid)
            if key in seen:
                log(f"pid {pid}: terminal {key} already nudged this tick -- skipped")
                continue
            seen.add(key)
            group = (every(due_fields[pid]), "once_at" in due_fields[pid])
            groups.setdefault(group, {})[key] = pid
        else:
            log(f"pid {pid}: gone or not a claude/codex/grok terminal -- skipped")
    stamp = datetime.now().astimezone()
    for (minutes, one_time), targets in groups.items():
        message = cfg["message"].format(
            now=stamp.strftime("%H:%M:%S %Z"),
            next=("none (one-time nudge)" if one_time else
                  (stamp + timedelta(minutes=minutes)).strftime("%H:%M:%S %Z")),
        )
        if WINDOWS:
            for pid in targets.values():
                deliver_console(pid, message)
            continue
        left = dict(targets)
        for channel in (deliver_tmux, deliver_wezterm, deliver_iterm2):
            if left:
                for tty in channel(left, message):
                    left.pop(tty, None)
        for tty, pid in left.items():
            log(
                f"{tty} (pid {pid}): no delivery channel (not tmux, WezTerm or iTerm2) -- skipped"
            )


# --- scheduler --------------------------------------------------------------------


def python_for_scheduler() -> str:
    if WINDOWS:
        w = Path(sys.executable).with_name("pythonw.exe")
        return str(w if w.exists() else sys.executable)
    # A PATH entry like /opt/homebrew/bin/python3 survives upgrades better
    # than the versioned sys.executable.
    return shutil.which("python3") or sys.executable


def render(name: str, xml: bool = False) -> str:
    cfg = config()
    values = {
        "PYTHON": python_for_scheduler(),
        "SCRIPT": str(SCRIPT),
        "LABEL": LAUNCHD_LABEL,
        "NAME": NAME,
        "STATE_DIR": str(STATE),
        "CRON_MARK": CRON_MARK,
        "RUN_LEVEL": "HighestAvailable" if cfg["elevated"] else "LeastPrivilege",
    }
    text = (TEMPLATES / name).read_text()
    for k, v in values.items():
        if xml:
            v = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace("{{" + k + "}}", v)
    return text


def backend() -> str:
    if SYSTEM == "Darwin":
        return "launchd"
    if WINDOWS:
        return "schtasks"
    if run(["systemctl", "--user", "show-environment"]).returncode == 0:
        return "systemd"
    return "cron"


def launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_dir() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd"
        / "user"
    )


def crontab_lines() -> list[str]:
    out = run(["crontab", "-l"])
    return out.stdout.splitlines() if out.returncode == 0 else []


def installed() -> bool:
    """True when the job exists, is loaded and matches what the templates render now."""
    b = backend()
    if b == "launchd":
        p = launchd_plist()
        return (
            p.exists()
            and p.read_text() == render("launchd.plist", xml=True)
            and run(
                ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
            ).returncode
            == 0
        )
    if b == "systemd":
        d = systemd_dir()
        return (
            all(
                (d / f"{NAME}.{ext}").exists()
                and (d / f"{NAME}.{ext}").read_text() == render(f"systemd.{ext}")
                for ext in ("service", "timer")
            )
            and run(
                ["systemctl", "--user", "is-active", f"{NAME}.timer"]
            ).stdout.strip()
            == "active"
        )
    if b == "cron":
        return render("crontab.txt").strip() in crontab_lines()
    out = run(["schtasks", "/Query", "/TN", NAME, "/XML"])
    return (
        out.returncode == 0
        and str(SCRIPT) in out.stdout
        and python_for_scheduler() in out.stdout
    )


def install() -> str:
    b = backend()
    STATE.mkdir(parents=True, exist_ok=True)
    if b == "launchd":
        p = launchd_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        p.write_text(render("launchd.plist", xml=True))
        out = run(["launchctl", "bootstrap", domain, str(p)])
        if out.returncode:
            raise SystemExit(f"launchctl bootstrap failed: {out.stderr.strip()}")
        return f"launchd agent {LAUNCHD_LABEL} ({p})"
    if b == "systemd":
        d = systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        for ext in ("service", "timer"):
            (d / f"{NAME}.{ext}").write_text(render(f"systemd.{ext}"))
        run(["systemctl", "--user", "daemon-reload"])
        out = run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"])
        if out.returncode:
            raise SystemExit(f"systemctl enable failed: {out.stderr.strip()}")
        run(["systemctl", "--user", "restart", f"{NAME}.timer"])
        return f"systemd user timer {NAME}.timer ({d})"
    if b == "cron":
        if not shutil.which("crontab"):
            raise SystemExit(
                "No systemd user session and no crontab: cannot schedule the nudge."
            )
        lines = [l for l in crontab_lines() if not l.endswith(CRON_MARK)] + [
            render("crontab.txt").strip()
        ]
        out = run(["crontab", "-"], input="\n".join(lines) + "\n")
        if out.returncode:
            raise SystemExit(f"crontab failed: {out.stderr.strip()}")
        return "crontab entry"
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "task.xml"
        xml.write_text(render("windows-task.xml", xml=True), encoding="utf-16")
        out = run(["schtasks", "/Create", "/TN", NAME, "/XML", str(xml), "/F"])
    if out.returncode:
        raise SystemExit(
            f"schtasks /Create failed: {(out.stderr or out.stdout).strip()}"
        )
    return f"Task Scheduler task {NAME}"


def uninstall() -> str:
    b = backend()
    if b == "launchd":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        launchd_plist().unlink(missing_ok=True)
    elif b == "systemd":
        run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"])
        for ext in ("service", "timer"):
            p = systemd_dir() / f"{NAME}.{ext}"
            if p.exists():
                p.unlink()
        run(["systemctl", "--user", "daemon-reload"])
    elif b == "cron":
        lines = [l for l in crontab_lines() if not l.endswith(CRON_MARK)]
        run(["crontab", "-"], input="\n".join(lines) + "\n" if lines else "")
    else:
        run(["schtasks", "/Delete", "/TN", NAME, "/F"])
    return f"Removed the {b} job; opt-ins and log stay in {STATE}."


def ensure_installed() -> None:
    if not installed():
        print(f"Scheduler job was missing or out of date; installed {install()}.")
    if not VERIFIED.exists():
        print_questions(
            setup_questions(),
            "First-run setup (no end-to-end test has passed here yet)",
        )


# --- first-run setup questions ---------------------------------------------------
# Things the OS needs from the user before nudges can arrive. The agent asks
# each with its question tool, acts on the answer, then runs `test` until it
# passes; a pass writes VERIFIED and the questions stop.


def ask(header: str, question: str, options: list[tuple[str, str]]) -> str:
    lines = [f"ASK-USER [{header}]: {question}"]
    lines += [f"    - {label}: {then}" for label, then in options]
    return "\n".join(lines)


def in_multiplexer() -> bool:
    return bool(os.environ.get("TMUX")) or os.environ.get("TERM_PROGRAM") == "WezTerm"


def iterm2_running() -> bool:
    return (
        SYSTEM == "Darwin"
        and "iTerm.app/Contents/MacOS/iTerm2"
        in run(["ps", "axww", "-o", "command="]).stdout
    )


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def q_macos_automation() -> str:
    py = python_for_scheduler()
    return ask(
        "Permission",
        f"Ready to run the nudge test? Nudges are typed into iTerm2 through AppleScript, so "
        f"macOS will ask once whether {Path(py).name} ({py}) may control iTerm2; please click "
        "OK when that prompt appears. (If it was dismissed earlier: System Settings > Privacy "
        f"& Security > Automation > {Path(py).name} > turn on iTerm2.)",
        [
            ("Ready / allowed", "run `nudge.py test`"),
            (
                "No prompt appeared",
                "check the Automation list together; `tccutil reset AppleEvents` "
                "makes macOS ask again, then rerun `nudge.py test`",
            ),
        ],
    )


def q_channel() -> str:
    return ask(
        "Terminal",
        "This terminal cannot be typed into from a background job. Nudges reach agent sessions "
        "running in tmux or WezTerm"
        + (", or iTerm2" if SYSTEM == "Darwin" else "")
        + ". Do you want to run sessions that should be nudged inside one of those?",
        [
            (
                "Use tmux",
                "install tmux if missing (brew/apt/dnf) and start agent sessions inside it",
            ),
            ("Use another supported terminal", "start agent sessions there"),
            ("Leave it", "nudges will only be logged as skipped for this terminal"),
        ],
    )


def q_linger() -> str:
    user = os.environ.get("USER", "")
    return ask(
        "Linger",
        "The nudge runs as a systemd user timer, which stops when you log out -- but agent "
        "sessions inside a detached tmux keep running. Enable lingering so the timer keeps "
        "firing while you are logged out?",
        [
            ("Yes, enable linger", f"run `loginctl enable-linger {user}`"),
            ("No", "nudges only fire while logged in"),
        ],
    )


def q_cron() -> str:
    return ask(
        "Cron",
        "No systemd user session here, so the nudge was scheduled with crontab, but no cron "
        "daemon seems to be running. Start one?",
        [
            (
                "Start cron",
                "e.g. `sudo service cron start` (Debian/Ubuntu) or `sudo systemctl "
                "enable --now crond` (Fedora); then run `nudge.py test`",
            ),
            ("Skip", "the job will never fire until cron runs"),
        ],
    )


def q_windows_elevated() -> str:
    return ask(
        "Elevation",
        "This agent runs in an elevated (Administrator) terminal. The scheduled task runs "
        "without elevation by default and Windows will not let it type into an elevated "
        "console. Register the task with highest privileges?",
        [
            (
                "Yes",
                "run `nudge.py install --elevated` from this elevated terminal, then `nudge.py test`",
            ),
            (
                "No, I'll use normal terminals",
                "run agent sessions in non-elevated terminals",
            ),
        ],
    )


def setup_questions() -> list[str]:
    qs = []
    if SYSTEM == "Darwin":
        if iterm2_running() and not os.environ.get("TMUX"):
            qs.append(q_macos_automation())
        if os.environ.get("TERM_PROGRAM") not in (
            "iTerm.app",
            "WezTerm",
        ) and not os.environ.get("TMUX"):
            qs.append(q_channel())
    elif WINDOWS:
        if is_admin() and not config()["elevated"]:
            qs.append(q_windows_elevated())
    else:
        b = backend()
        if (
            b == "systemd"
            and "Linger=no"
            in run(
                [
                    "loginctl",
                    "show-user",
                    os.environ.get("USER", ""),
                    "--property=Linger",
                ]
            ).stdout
        ):
            qs.append(q_linger())
        if b == "cron" and not any(
            run(["pgrep", "-x", d]).returncode == 0 for d in ("cron", "crond", "cronie")
        ):
            qs.append(q_cron())
        if not in_multiplexer():
            qs.append(q_channel())
    return qs


def failure_questions(log_text: str) -> list[str]:
    """Questions that match why a test failed, read from its log lines."""
    qs = []
    if re.search(r"iterm2: osascript failed.*\((-1712|-1743|-10004)\)", log_text):
        qs.append(q_macos_automation())
    if re.search(r"AttachConsole failed \(5\)", log_text):
        qs.append(q_windows_elevated())
    if "no delivery channel" in log_text and not qs:
        qs.append(q_channel())
    if not log_text.strip() and backend() == "cron":
        qs.append(q_cron())
    return qs


def print_questions(qs: list[str], title: str) -> None:
    if qs:
        print(
            f"{title}: ask the user each question below, act on the answer, then run "
            "`nudge.py test` until it passes."
        )
        print("\n".join(qs))
    else:
        print(
            f"{title}: no OS setup questions; run `nudge.py test` to confirm nudges arrive."
        )


def trigger_now() -> bool:
    """Ask the scheduler to run the job now. False for cron (it runs within a minute)."""
    b = backend()
    if b == "launchd":
        return (
            run(
                ["launchctl", "kickstart", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
            ).returncode
            == 0
        )
    if b == "systemd":
        return (
            run(
                ["systemctl", "--user", "start", "--no-block", f"{NAME}.service"]
            ).returncode
            == 0
        )
    if b == "schtasks":
        return run(["schtasks", "/Run", "/TN", NAME]).returncode == 0
    return False


# --- end-to-end test --------------------------------------------------------------


def test_target(result: str) -> None:
    """Runs in the test terminal: record our pid, then every line typed at us
    until the test closes the terminal (or 3 minutes pass)."""
    with open(result, "a") as f:
        f.write(f"ready {os.getpid()}\n")
    deadline = time.time() + 180
    for line in sys.stdin:
        with open(result, "a") as f:
            f.write(f"line {line}")
        if time.time() > deadline:
            break


def spawn_target(channel: str, result: Path):
    """Open a throwaway terminal running test_target; returns a cleanup callable."""
    cmd = [sys.executable, str(SCRIPT), "_target", str(result), TEST_MARKER]
    if channel == "tmux":
        sess = f"{NAME}-test-{os.getpid()}"
        run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                sess,
                "-x",
                "250",
                "-y",
                "50",
                " ".join(map(sh_quote, cmd)),
            ]
        )
        return lambda: run(["tmux", "kill-session", "-t", sess])
    if channel == "wezterm":
        out = run(["wezterm", "cli", "spawn", "--new-window", "--"] + cmd)
        pane = out.stdout.strip()
        return lambda: run(["wezterm", "cli", "kill-pane", "--pane-id", pane])
    if channel == "iterm2":
        shell = " ".join(map(sh_quote, cmd))
        out = run(
            [
                "osascript",
                "-e",
                f'tell application "iTerm2" to id of (create window with '
                f"default profile command {applescript_str(shell)})",
            ]
        )
        wid = out.stdout.strip()
        return lambda: run(
            [
                "osascript",
                "-e",
                f'tell application "iTerm2" to repeat with w in windows\n'
                f"if id of w is {wid or -1} then close w\nend repeat",
            ]
        )
    if channel == "console":
        p = subprocess.Popen(cmd, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        return p.kill
    raise SystemExit(f"unknown channel {channel}")


def sh_quote(s: str) -> str:
    return (
        s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"
    )


def default_channel() -> str | None:
    if WINDOWS:
        return "console"
    if shutil.which("tmux"):
        return "tmux"
    if os.environ.get("TERM_PROGRAM") == "WezTerm" and shutil.which("wezterm"):
        return "wezterm"
    if (
        SYSTEM == "Darwin"
        and "iTerm.app/Contents/MacOS/iTerm2"
        in run(["ps", "axww", "-o", "command="]).stdout
    ):
        return "iterm2"
    return None


def wait_for(
    path: Path, pattern: str, seconds: float, start: int = 0
) -> re.Match | None:
    """Poll path's text from offset start until pattern matches."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        m = re.search(pattern, path.read_text()[start:] if path.exists() else "")
        if m:
            return m
        time.sleep(0.5)
    return None


def e2e_test(channel: str | None, pid: int | None) -> bool:
    """Fire the real scheduler job at one terminal and check the message arrived."""
    b = backend()
    log_start = len(LOG.read_text()) if LOG.exists() else 0
    cleanup = None
    if pid:
        p = is_agent_tui(pid)
        if not p:
            print(f"pid {pid} is not a claude/codex/grok process in a terminal.")
            return False
        sent_re = rf"{re.escape(p['tty'])}\)?: sent|pid {pid}: sent"
        result = None
    else:
        channel = channel or default_channel()
        if not channel:
            print(
                "No test channel available here (needs tmux, WezTerm, iTerm2 or Windows)."
            )
            return False
        result = STATE / f"test-{os.getpid()}.out"
        result.unlink() if result.exists() else None
        cleanup = spawn_target(channel, result)
        m = wait_for(result, r"ready (\d+)", 15)
        if not m:
            cleanup()
            print(f"Test terminal ({channel}) did not start.")
            return False
        pid = int(m.group(1))
    try:
        TEST_REQUEST.write_text(f"{pid} test\n")
        how = "triggered" if trigger_now() else "waiting for the next cron minute"
        print(
            f"Test target pid {pid} via {channel or 'existing terminal'}; {b} job {how}..."
        )
        if result:
            ok = bool(
                wait_for(result, r"line .*Automated nudge", 90 if b == "cron" else 30)
            )
            # The job logs "sent" only after its trailing CR; close the
            # terminal after that.
            wait_for(LOG, r"(?s)test fire.*(: sent|skipped)", 10, log_start)
        else:
            deadline = time.time() + (90 if b == "cron" else 30)
            ok = False
            while time.time() < deadline and not ok:
                time.sleep(1)
                ok = bool(wait_for(LOG, sent_re, 0.1, log_start))
    finally:
        if TEST_REQUEST.exists():
            TEST_REQUEST.unlink()
        if cleanup:
            time.sleep(0.5)
            cleanup()
        if result and result.exists():
            result.unlink()
    tail = LOG.read_text()[log_start:].strip() if LOG.exists() else ""
    print(
        "PASS: the scheduled job typed the nudge and it was submitted."
        if ok
        else "FAIL: no nudge arrived."
    )
    if tail:
        print("log:\n  " + tail.replace("\n", "\n  "))
    if ok:
        VERIFIED.write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} {b} {channel or 'existing terminal'}\n"
        )
    else:
        print_questions(failure_questions(tail), "To fix")
    return ok


# --- cli --------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]
    if {"-h", "--help", "help"} & set(args):
        return print(__doc__.strip())
    action = args[0] if args and not args[0].startswith("-") else "status"

    def opt(name: str) -> str | None:
        if name not in args:
            return None
        try:
            return args[args.index(name) + 1]
        except IndexError:
            sys.exit(f"{name} needs a value")

    if action == "_target":
        return test_target(args[1])
    if action == "fire":
        return fire()
    if action == "uninstall":
        return print(uninstall())

    pid = opt("--pid")
    if pid is not None and not pid.isdigit():
        sys.exit("--pid needs a number")
    pid = int(pid) if pid else None

    if action == "install":
        interval = opt("--interval")
        if interval:
            save_config(interval_minutes=int(interval))
        if "--elevated" in args:
            save_config(elevated=True)
        print(
            f"Installed {install()}; default pace for new opt-ins: every "
            f"{config()['interval_minutes']} min."
        )
        if not VERIFIED.exists():
            print_questions(setup_questions(), "First-run setup")
        return

    action = {"on": "enable", "off": "disable"}.get(action, action)
    if action == "test":
        if not installed():
            print(f"Scheduler job was missing or out of date; installed {install()}.")
        sys.exit(0 if e2e_test(opt("--channel"), pid) else 1)
    once_at = None
    if action == "once":
        at = opt("--at")
        if not at:
            sys.exit("once needs --at HH:MM (local time)")
        try:
            once_at = next_local_time(at)
        except ValueError as error:
            sys.exit(str(error))
    if action not in ("status", "enable", "disable", "once"):
        sys.exit(__doc__.strip())
    # Turning a nudge off must not install a background job or start setup.
    if action != "disable":
        ensure_installed()

    pace = opt("--every")
    if pace is not None and not (pace.isdigit() and int(pace) >= 1):
        sys.exit("--every needs a whole number of minutes, at least 1")

    explicit_pid = pid is not None
    found = find_target(pid)
    if not found:
        return
    pid, tty = found
    if action == "once":
        try:
            schedule_once(pid, tty, once_at)
        except RuntimeError as error:
            sys.exit(str(error))
        when = datetime.fromtimestamp(once_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        print(f"One-time keepalive nudge for terminal {tty} (pid {pid}): scheduled for {when}.")
        return
    if action in ("enable", "disable"):
        with state_lock() as locked:
            if not locked:
                sys.exit("The nudge state is busy (a delivery is running); try again in a minute.")
            entries = listed()
            if action == "enable":
                f = entries.get(pid, {})
                f.pop("once_at", None)
                f["every"] = pace or f.get("every") or str(config()["interval_minutes"])
                f.setdefault("last", str(int(time.time())))  # first nudge one interval from now
                f["tty"] = tty
                session = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "")[:8]
                if session:
                    f["session"] = session
                # One opt-in per terminal: a new one replaces any other on it.
                if not WINDOWS:
                    for other in [q for q, g in entries.items() if q != pid and g.get("tty") == tty]:
                        del entries[other]
                        print(f"(replaced the earlier opt-in of pid {other} on {tty})")
                entries[pid] = f
            else:
                entries.pop(pid, None)
            write_listed(entries)
    f = listed().get(pid)
    p = proc(pid)
    cmd = (p or {}).get("command", "")
    exe = (
        cmd[1:].split('"', 1)[0] if cmd.startswith('"') else (cmd.split() or ["pid"])[0]
    )
    name = re.split(r"[/\\]", exe)[-1]
    who = (
        f"terminal {tty} ({name} pid {pid})"
        if explicit_pid
        else f"this session (terminal {tty}, {name} pid {pid})"
    )
    if f:
        if "once_at" in f:
            when = datetime.fromtimestamp(float(f["once_at"])).astimezone()
            print(f"One-time nudge for {who}: scheduled for {when:%Y-%m-%d %H:%M %Z}.")
            return
        nxt = datetime.fromtimestamp(float(f.get("last", time.time())) + every(f) * 60)
        print(
            f"Keepalive nudge for {who}: enabled, every {every(f)} min; next around {nxt:%H:%M}."
        )
    else:
        print(f"Keepalive nudge for {who}: disabled.")


if __name__ == "__main__":
    main()
