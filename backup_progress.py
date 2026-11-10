"""
Backup progress reporting shared by the backup engine, the `--watch` command and the MCP
server (`backup_progress.py`).

The engine (backup_runner.py) writes `.backup_status.json` in the private state directory.
This module turns that file into what a person on the couch actually wants to hear:

  * one line the moment something changes ("Passcode accepted, backup started"),
  * a short check-in every few minutes ("12.4 GB copied, 46%, 31 MB/s, ~7 min left"),
  * a loud line if the engine died or stalled, instead of silence.

`watch()` prints exactly those lines and exits when the backup ends, so it works as an
event stream for any agent harness (e.g. Claude Code's Monitor tool) or in a terminal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

from common import GB, state_path

STATUS_FILE = state_path(".backup_status.json")

# Phases written by BackupRunner, in order.
TERMINAL_PHASES = {"completed", "failed"}
PHASE_LINES = {
    "preflight": "Checking the phone, disk space and encryption...",
    "starting": "Connecting to the phone's backup service...",
    "waiting_passcode": "ACTION NEEDED: enter your passcode on the iPhone to start the backup.",
    "streaming": "Passcode accepted. Backup started and copying data from the phone.",
    "stream_done": "All data copied from the phone. Finishing up...",
    "verifying": "Copy finished. Verifying the backup (encryption, password, integrity)...",
    "cloning": "Backup verified. Making the safety copy (on Windows a full copy: a few minutes)...",
    "completed": "Backup complete and verified.",
    "failed": "Backup FAILED.",
}
# No status update for this long while the engine says it's busy = report a stall.
STALL_AFTER_SEC = 180


# ---------------------------------------------------------------------------
# Status file I/O
# ---------------------------------------------------------------------------

def write_status(path: Path, payload: Dict[str, Any]) -> None:
    """Atomic write (temp file + rename) so readers never see half a JSON document."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_status(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    try:
        with open(path or STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pid_alive(pid: Optional[int]) -> Optional[bool]:
    """True/False if we can tell, None if unknown. Never signals the process: on Windows,
    os.kill(pid, 0) would terminate it, so use OpenProcess there."""
    if not pid:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return None


def _age_seconds(iso: Optional[str], now: Optional[float] = None) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now if now is not None else time.time()) - t.timestamp()


# ---------------------------------------------------------------------------
# Rate / ETA
# ---------------------------------------------------------------------------

class ProgressEstimator:
    """Turns (time, bytes written, device-reported %) samples into a rate and an ETA.

    The phone's own overall percentage (idevicebackup2 "NN% Finished" lines) is preferred
    for the ETA. Before it moves, fall back to bytes written vs. the estimated total (the
    phone's used storage). Rate is smoothed over a sliding window.
    """

    def __init__(self, estimated_total_bytes: Optional[int] = None, window_sec: float = 120.0):
        self.estimated_total_bytes = estimated_total_bytes or None
        self.window_sec = window_sec
        self.samples: List[Tuple[float, int]] = []
        self.started: Optional[float] = None

    def add(self, t: float, bytes_written: int) -> None:
        if self.started is None:
            self.started = t
        self.samples.append((t, max(0, bytes_written)))
        while len(self.samples) > 2 and t - self.samples[0][0] > self.window_sec:
            self.samples.pop(0)

    def rate_bps(self) -> Optional[float]:
        if len(self.samples) < 2:
            return None
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else None

    def eta_seconds(self, overall_percent: Optional[float]) -> Optional[float]:
        if not self.samples or self.started is None:
            return None
        now, written = self.samples[-1]
        elapsed = now - self.started
        if overall_percent and 2 <= overall_percent < 100 and elapsed > 0:
            return elapsed * (100 - overall_percent) / overall_percent
        rate = self.rate_bps()
        if self.estimated_total_bytes and rate and rate > 0 and written < self.estimated_total_bytes:
            return (self.estimated_total_bytes - written) / rate
        return None


# ---------------------------------------------------------------------------
# Human summaries
# ---------------------------------------------------------------------------

def _fmt_duration(sec: Optional[float]) -> str:
    if sec is None or sec < 0:
        return "?"
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 3600:
        return f"{round(sec / 60)} min"
    return f"{sec // 3600} h {round((sec % 3600) / 60)} min"


def progress_line(status: Dict[str, Any]) -> str:
    """One short check-in sentence for a streaming backup."""
    parts: List[str] = []
    written = status.get("bytes_written")
    if isinstance(written, (int, float)):
        total = status.get("estimated_total_bytes")
        est = f" of ~{total / GB:.0f} GB" if isinstance(total, (int, float)) and total > 0 else ""
        parts.append(f"{written / GB:.1f} GB copied{est}")
    pct = status.get("overall_percent")
    if isinstance(pct, (int, float)):
        parts.append(f"{pct:.0f}%")
    rate = status.get("rate_bps")
    if isinstance(rate, (int, float)) and rate > 0:
        parts.append(f"{rate / 1e6:.0f} MB/s")
    eta = status.get("eta_seconds")
    if isinstance(eta, (int, float)):
        parts.append(f"~{_fmt_duration(eta)} left")
    elapsed = _age_seconds(status.get("streaming_started_at"))
    if elapsed is not None:
        parts.append(f"running {_fmt_duration(elapsed)}")
    return "Backup in progress: " + (", ".join(parts) if parts else "copying...")


def describe(status: Optional[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """Status snapshot plus liveness and a ready-to-say `summary` sentence."""
    if not status:
        return {"phase": None, "running": False, "summary": "No backup has been started on this computer yet."}
    phase = status.get("phase")
    alive = pid_alive(status.get("pid"))
    age = _age_seconds(status.get("updated_at"), now)
    terminal = phase in TERMINAL_PHASES
    out = dict(status)
    out.pop("udid", None)  # nothing downstream needs it; keeps agent transcripts clean
    out["running"] = bool(not terminal and alive is not False)
    out["stalled"] = bool(not terminal and alive and age is not None and age > STALL_AFTER_SEC
                          and phase not in ("waiting_passcode", "cloning"))  # a full copy writes no status
    out["engine_died"] = bool(not terminal and alive is False)

    if out["engine_died"]:
        summary = ("The backup engine stopped unexpectedly "
                   f"(last step: {phase}). Re-run the backup; the phone was not changed.")
    elif out["stalled"]:
        summary = (f"Backup looks stuck: no progress for {_fmt_duration(age)}. Check the cable and "
                   "that the phone is unlocked.")
    elif phase == "streaming":
        summary = progress_line(status)
    elif phase == "failed":
        summary = f"Backup FAILED: {status.get('error') or status.get('message') or 'unknown error'}"
    elif phase == "completed":
        took = status.get("duration_seconds")
        summary = "Backup complete and verified" + (f" (took {_fmt_duration(took)})." if took else ".")
    else:
        summary = PHASE_LINES.get(phase or "", status.get("message") or str(phase))
    out["summary"] = summary
    return out


# ---------------------------------------------------------------------------
# Watcher (event stream)
# ---------------------------------------------------------------------------

def watch(
    status_file: Optional[Path] = None,
    checkin_minutes: float = 5.0,
    poll_sec: float = 5.0,
    wait_for_start_sec: float = 120.0,
    out: TextIO = sys.stdout,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Prints one line per event and exits when the backup ends.

    Events: every phase change (incl. the passcode prompt and "backup started"), a progress
    check-in every `checkin_minutes` while copying, and a warning if the engine stalls or
    dies. Exit code: 0 completed, 1 failed/died, 2 nothing to watch.
    """
    path = status_file or STATUS_FILE

    def emit(line: str) -> None:
        stamp = datetime.now().strftime("%H:%M")
        print(f"[{stamp}] {line}", file=out, flush=True)

    started = clock()
    snap = describe(read_status(path))
    # Nothing running yet (the engine may still be starting): wait a little for it.
    while not snap["running"] and clock() - started < wait_for_start_sec:
        sleep(poll_sec)
        snap = describe(read_status(path))
    if not snap["running"]:
        emit(snap["summary"] if snap.get("phase") is None else f"No backup running. Last one: {snap['summary']}")
        return 2

    last_phase: Optional[str] = None
    last_checkin = clock()
    warned_stall = False
    while True:
        phase = snap.get("phase")
        if phase != last_phase:
            stream_age = _age_seconds(snap.get("streaming_started_at"))
            joined_late = last_phase is None and phase == "streaming" and (stream_age is None or stream_age > 120)
            if phase in TERMINAL_PHASES or joined_late:
                emit(snap["summary"])  # attached mid-backup: say where it is, not "started"
            else:
                emit(PHASE_LINES.get(phase or "", snap["summary"]))
            last_phase = phase
            last_checkin = clock()
        elif phase == "streaming" and clock() - last_checkin >= checkin_minutes * 60:
            emit(snap["summary"])
            last_checkin = clock()

        if snap["engine_died"]:
            emit(snap["summary"])
            return 1
        if snap["stalled"] and not warned_stall:
            emit(snap["summary"])
            warned_stall = True
        elif not snap["stalled"]:
            warned_stall = False
        if phase in TERMINAL_PHASES:
            return 0 if phase == "completed" else 1

        sleep(poll_sec)
        snap = describe(read_status(path))
