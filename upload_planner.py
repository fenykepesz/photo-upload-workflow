#!/usr/bin/env python3
"""
Daily upload planner: starts the day's upload at a random time, unattended.

Until 2026-11 Erik started every upload by telling Hermes to run it. Now:
- A daily Hermes job runs `upload_planner.py` in the morning. If an Approved
  row is due today (same rule as a batch run: today, or up to 7 days late),
  it picks a random start time in WINDOW (Israel time) and creates ONE Hermes
  one-off job for it, then sends "📅 Today's upload: ..." to Telegram. With
  nothing due it sends "📭 No upload today" (Erik wants that message too).
- The one-off runs `upload_planner.py --run`, which waits for the upload lock
  if something else (e.g. YouPic catch-up) holds it, then runs
  run_upload.sh — the same real upload Hermes starts by hand, with the usual
  Telegram reports and verify_upload afterwards.
- Pause: create PAUSE_FILE (`--pause` / `--resume`). The planner then only
  sends "⏸ Uploads paused"; a pending one-off also checks it before starting.
- Idempotent: running the planner again the same day keeps the existing
  one-off. If the day's row was already uploaded by hand, the one-off finds
  nothing due and says so instead of running.

Usage:
  upload_planner.py              plan today (cron entry point)
  upload_planner.py --run        start the planned upload (one-off entry point)
  upload_planner.py --dry-run    show what would be planned; no job, no message
  upload_planner.py --status     show state and pause flag
  upload_planner.py --pause | --resume
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import upload as u  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
WINDOW = ((12, 0), (15, 0))               # local start/end of the random start time
LOCK_FILE = SCRIPT_DIR / ".upload.lock"
PAUSE_FILE = SCRIPT_DIR / ".uploads_paused"
STATE_FILE = SCRIPT_DIR / "logs" / "upload_planner_state.json"
LOG_FILE = SCRIPT_DIR / "logs" / "upload_planner.log"
HERMES = "/usr/local/bin/hermes"
JOB_SCRIPT = "scheduled_upload.sh"        # under ~/.hermes/scripts/
ONEOFF_NAME = "Photo Upload (scheduled one-off)"
LOCK_WAIT_MIN = 45                        # max wait for a busy lock before giving up


def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    print(msg)


def notify(msg):
    """Telegram (Photo Uploads channel, like the upload summaries). Never raises."""
    try:
        u._send_telegram_message(msg, parse_mode=None)
    except Exception:
        pass


def now_local():
    return datetime.now(timezone.utc).astimezone(TZ)


def fmt_local(dt):
    return dt.astimezone(TZ).strftime("%H:%M")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1))


def due_rows():
    return u.filter_rows(u.load_queue(u.DEFAULT_CSV))


def describe(rows):
    parts = []
    for r in rows:
        title = (r.get("title") or "").strip()
        parts.append(f'{r["upload_id"]} "{title}"' if title else r["upload_id"])
    return ", ".join(parts)


def next_approved():
    """Earliest future Approved row (for the 'nothing today' message)."""
    today = now_local().date()
    best = None
    for r in u.load_queue(u.DEFAULT_CSV):
        if r.get("status", "").strip() != "Approved":
            continue
        d = u._parse_scheduled(r.get("scheduled_date", "").strip(), "")
        if d and d.date() > today and (best is None or d < best[0]):
            best = (d, r)
    return best


# ── Hermes one-off job ─────────────────────────────────────────
def _hermes(*args):
    return subprocess.run([HERMES, "cron", *args], capture_output=True, text=True, timeout=60)


def _job_pending(job_id):
    try:
        out = _hermes("list").stdout
    except Exception:
        return False
    i = out.find(job_id)
    return i >= 0 and "[active]" in out[i:i + 40]


def create_oneoff(run_at):
    """Create the one-off job for `run_at` (aware datetime). Returns job id."""
    minutes = max(1, round((run_at - datetime.now(timezone.utc)).total_seconds() / 60))
    res = _hermes("create", f"in {minutes}m", "--name", ONEOFF_NAME, "--script", JOB_SCRIPT,
                  "--no-agent", "--deliver", "local", "--failure-deliver", "telegram:718581332")
    m = re.search(r"\b([0-9a-f]{12})\b", res.stdout + res.stderr)
    if res.returncode != 0 or not m:
        raise RuntimeError((res.stdout + res.stderr).strip()[-200:])
    return m.group(1)


def pick_time(local_now):
    """Random start in today's window; if the window has (mostly) passed, soon."""
    day = local_now.date()
    start = datetime(day.year, day.month, day.day, *WINDOW[0], tzinfo=TZ)
    end = datetime(day.year, day.month, day.day, *WINDOW[1], tzinfo=TZ)
    earliest = max(start, local_now + timedelta(minutes=5))
    if earliest >= end:
        return local_now + timedelta(minutes=5)
    secs = int((end - earliest).total_seconds())
    return earliest + timedelta(seconds=random.randint(0, secs))


# ── Modes ──────────────────────────────────────────────────────
def plan(dry_run=False):
    local_now = now_local()
    today = local_now.date().isoformat()
    st = load_state()
    rows = due_rows()

    if PAUSE_FILE.exists():
        log(f"plan {today}: paused ({describe(rows) or 'nothing due'})")
        if not dry_run:
            notify("⏸ Uploads paused — nothing scheduled today"
                   + (f" ({describe(rows)} waiting)." if rows else ".")
                   + " Resume: upload_planner.py --resume")
        return 0

    if not rows:
        nxt = next_approved()
        tail = (f" Next approved: {nxt[1]['upload_id']} on {nxt[0]:%a %d %b}." if nxt
                else " No approved rows in the queue.")
        log(f"plan {today}: nothing due")
        if not dry_run:
            notify("📭 No upload today." + tail)
        return 0

    if st.get("date") == today and st.get("job_id") and _job_pending(st["job_id"]):
        log(f"plan {today}: already scheduled ({st['job_id']} at {st['run_at']})")
        return 0

    run_at = pick_time(local_now)
    if dry_run:
        print(f"would schedule {describe(rows)} at {run_at:%Y-%m-%d %H:%M %Z}")
        return 0
    try:
        job_id = create_oneoff(run_at)
    except Exception as e:
        log(f"plan {today}: schedule FAILED: {e}")
        notify(f"⚠️ Couldn't schedule today's upload ({describe(rows)}): {e}. Start it by hand via Hermes.")
        return 1
    save_state({"date": today, "job_id": job_id, "run_at": run_at.isoformat(timespec="minutes"),
                "rows": [r["upload_id"] for r in rows]})
    log(f"plan {today}: {describe(rows)} at {fmt_local(run_at)} (job {job_id})")
    notify(f"📅 Today's upload: {describe(rows)} at {fmt_local(run_at)}.")
    return 0


def lock_busy():
    if not LOCK_FILE.exists():
        return False
    try:
        os.kill(int(LOCK_FILE.read_text().strip()), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False       # stale lock — run_upload.sh clears it itself
    except PermissionError:
        return True


def run():
    if PAUSE_FILE.exists():
        log("run: paused — skipped")
        notify("⏸ Scheduled upload skipped — uploads are paused.")
        return 0
    rows = due_rows()
    if not rows:
        log("run: nothing due (already uploaded by hand?)")
        notify("ℹ️ Scheduled upload: nothing left to upload today (already done?).")
        return 0
    waited = 0
    while lock_busy() and waited < LOCK_WAIT_MIN:
        time.sleep(60)
        waited += 1
    if lock_busy():
        log(f"run: lock still busy after {LOCK_WAIT_MIN} min — gave up")
        notify(f"⚠️ Scheduled upload of {describe(rows)} didn't start: another upload held the lock "
               f"for {LOCK_WAIT_MIN} min. Start it by hand via Hermes.")
        return 1
    log(f"run: starting {describe(rows)}" + (f" (waited {waited} min for lock)" if waited else ""))
    rc = subprocess.call(["./run_upload.sh"], cwd=SCRIPT_DIR,
                         stdout=open(SCRIPT_DIR / "logs" / "scheduled_upload.log", "a"),
                         stderr=subprocess.STDOUT)
    log(f"run: run_upload.sh exited {rc}")
    return 0


def status():
    print("paused:", PAUSE_FILE.exists())
    print("state:", json.dumps(load_state(), indent=1))
    print("due now:", describe(due_rows()) or "nothing")
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--pause", action="store_true")
    g.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    if args.pause:
        PAUSE_FILE.touch()
        log("paused")
        notify("⏸ Scheduled uploads paused. Resume: upload_planner.py --resume")
        return 0
    if args.resume:
        PAUSE_FILE.unlink(missing_ok=True)
        log("resumed")
        notify("▶️ Scheduled uploads resumed.")
        return 0
    if args.status:
        return status()
    if args.run:
        return run()
    return plan(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
