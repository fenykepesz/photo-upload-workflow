#!/usr/bin/env python3
"""
Login catch-up: retry platforms a normal run skipped because they were
logged out at pre-flight.

Since 2026-11-04 uploads start unattended (upload_planner.py), so a logged-out
site no longer stops the run: upload.py re-checks it twice, then uploads the
rest and calls `login_catchup.py --add ROW PLATFORM`. This script:
- keeps the pending row+platform pairs in logs/login_catchup_state.json and
  ONE Hermes one-off job, RETRY_HOURS from now;
- on each run, for every pending pair (oldest first) runs
  `upload.py --row ROW --platform PLATFORM` with LOGIN_CATCHUP=1. A
  single-platform run checks only that platform's login and still stops if
  it's logged out, so nothing is posted until Erik has logged back in;
- success → Telegram ✅ + verify_upload; still logged out → try again in
  RETRY_HOURS, until GIVE_UP_HOURS after it was first skipped, then one
  Telegram message and the pair is dropped; any other failure counts as an
  attempt, MAX_ATTEMPTS then give up.
- Busy upload lock → retry in 15 min. The row's original upload_timestamp is
  restored afterwards, and the stale "<Platform>: deferred — logged out" note
  is removed from error_log on success.
History: logs/login_catchup.log

Usage:
  login_catchup.py --add ROW PLATFORM   queue a pair + make sure a run is scheduled
  login_catchup.py                      retry pending pairs (one-off entry point; also by hand
                                        once you've logged in, to skip the wait)
  login_catchup.py --status
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import upload as u  # noqa: E402
from youpic_catchup import _hermes, _job_pending, lock_busy, local  # noqa: E402

LOCK_FILE = SCRIPT_DIR / ".upload.lock"
STATE_FILE = SCRIPT_DIR / "logs" / "login_catchup_state.json"
LOG_FILE = SCRIPT_DIR / "logs" / "login_catchup.log"
JOB_SCRIPT = "login_catchup.sh"            # under ~/.hermes/scripts/
ONEOFF_NAME = "Login Catch-up (one-off)"
RETRY_HOURS = 3
GIVE_UP_HOURS = 24
MAX_ATTEMPTS = 3
URL_FIELD = {"500PX": "url_500px", "35P": "url_35p", "YOUPIC": "url_youpic", "VK": "url_vk", "X": "url_x",
             "BSKY": "url_bsky", "FB": "url_fb", "IG": "url_ig", "DA": "da_deviation_url"}


def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    print(msg)


def notify(msg):
    try:
        u._send_telegram_message(msg, parse_mode=None)
    except Exception:
        pass


def name(p):
    return u.PLATFORM_NAMES.get(p, p)


def load_state():
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    st.setdefault("pending", {})          # "ROW|PLAT": {"since": iso, "attempts": n}
    st.setdefault("scheduled", None)      # {"job_id", "run_at"}
    return st


def save_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1))


def ensure_scheduled(st, when):
    """Exactly one pending one-off at ~`when`. Never removes a job whose time
    has passed (it may be running — `hermes cron remove` kills a running job)."""
    cur = st.get("scheduled") or {}
    if cur.get("job_id") and _job_pending(cur["job_id"]):
        cur_at = datetime.fromisoformat(cur["run_at"])
        if cur_at <= datetime.now() + timedelta(minutes=2) or abs((cur_at - when).total_seconds()) < 600:
            return cur_at
        _hermes("remove", cur["job_id"])
    minutes = max(2, int((when - datetime.now()).total_seconds() // 60) + 1)
    try:
        res = _hermes("create", f"in {minutes}m", "--name", ONEOFF_NAME, "--script", JOB_SCRIPT,
                      "--no-agent", "--deliver", "local", "--failure-deliver", "telegram:718581332")
        m = re.search(r"\b([0-9a-f]{12})\b", res.stdout + res.stderr)
        if res.returncode != 0 or not m:
            raise RuntimeError((res.stdout + res.stderr).strip()[-200:])
        run_at = datetime.now() + timedelta(minutes=minutes)
        st["scheduled"] = {"job_id": m.group(1), "run_at": run_at.isoformat(timespec="seconds")}
        log(f"scheduled next run at {run_at:%Y-%m-%d %H:%M} UTC (job {m.group(1)})")
        return run_at
    except Exception as e:
        st["scheduled"] = None
        log(f"schedule FAILED: {e}")
        notify(f"⚠️ Login catch-up couldn't schedule its retry ({str(e)[:120]}). "
               "Run login_catchup.py by hand after logging in.")
        return None


def add(st, row_id, plat):
    key = f"{row_id}|{plat.upper()}"
    if key not in st["pending"]:
        st["pending"][key] = {"since": datetime.now().isoformat(timespec="seconds"), "attempts": 0}
        log(f"{key}: added (logged out at pre-flight)")
    ensure_scheduled(st, datetime.now() + timedelta(hours=RETRY_HOURS))


def run(st):
    if not st["pending"]:
        return
    if lock_busy():
        log("upload lock busy — retry in 15 min")
        ensure_scheduled(st, datetime.now() + timedelta(minutes=15))
        return
    rows = {r["upload_id"]: r for r in u.load_queue(u.DEFAULT_CSV)}
    for key in sorted(st["pending"], key=lambda k: st["pending"][k]["since"]):
        row_id, plat = key.split("|")
        info = st["pending"][key]
        row = rows.get(row_id)
        if not row:
            log(f"{key}: row gone from the queue — dropped")
            st["pending"].pop(key)
            continue
        if row.get(URL_FIELD[plat], "").strip():
            log(f"{key}: already has a URL (posted by hand?) — dropped")
            st["pending"].pop(key)
            continue
        if lock_busy():
            break
        orig_ts = row.get("upload_timestamp", "")
        raw = SCRIPT_DIR / "logs" / f"raw_login_catchup_{datetime.now():%Y%m%d_%H%M%S}.log"
        env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"), LOGIN_CATCHUP="1")
        LOCK_FILE.write_text(str(os.getpid()))
        try:
            with open(raw, "w") as fh:
                rc = subprocess.call([sys.executable, "upload.py", "--row", row_id, "--platform", plat],
                                     cwd=SCRIPT_DIR, stdout=fh, stderr=subprocess.STDOUT, env=env)
        finally:
            LOCK_FILE.unlink(missing_ok=True)
        out = raw.read_text(errors="replace")
        fresh = next((r for r in u.load_queue(u.DEFAULT_CSV) if r["upload_id"] == row_id), {})
        url = fresh.get(URL_FIELD[plat], "").strip()
        title = (row.get("title") or "").strip()
        label = f'{row_id} "{title}"' if title else row_id
        fix = {"upload_timestamp": orig_ts}
        if url:
            st["pending"].pop(key)
            kept = [e for e in fresh.get("error_log", "").split("; ") if e and not e.startswith(name(plat) + ":")]
            fix["error_log"] = "; ".join(kept)
            u.save_row_update(u.DEFAULT_CSV, row_id, fix)
            log(f"{key}: SUCCESS {url}")
            notify(f"✅ Login catch-up: {label} is now on {name(plat)}.\n{url}\nRow status: {fresh.get('status', '?')}.")
            subprocess.Popen([sys.executable, "verify_upload.py", "--row", row_id, "--platforms", plat,
                              "--delay", "60", "--recheck", "180"], cwd=SCRIPT_DIR, env=env,
                             stdout=open(SCRIPT_DIR / "logs" / "verify_run.log", "a"), stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            continue
        u.save_row_update(u.DEFAULT_CSV, row_id, fix)
        age_h = (datetime.now() - datetime.fromisoformat(info["since"])).total_seconds() / 3600
        if "Not logged in to" in out:
            if age_h >= GIVE_UP_HOURS:
                st["pending"].pop(key)
                log(f"{key}: still logged out after {age_h:.0f}h — GAVE UP")
                notify(f"⚠️ Login catch-up gave up on {name(plat)} for {label}: still logged out after "
                       f"{GIVE_UP_HOURS}h. After logging in: upload.py --row {row_id} --platform {plat}")
            else:
                log(f"{key}: still logged out ({age_h:.1f}h) — retry in {RETRY_HOURS}h")
            continue
        info["attempts"] += 1
        m = re.search(r": FAILED[^—]*— (.+)", out)
        reason = m.group(1).strip()[:200] if m else f"see logs/{raw.name}"
        log(f"{key}: attempt {info['attempts']}/{MAX_ATTEMPTS} failed (rc={rc}): {reason}")
        if info["attempts"] >= MAX_ATTEMPTS or age_h >= GIVE_UP_HOURS:
            st["pending"].pop(key)
            notify(f"⚠️ Login catch-up gave up on {name(plat)} for {label} after {info['attempts']} "
                   f"failed attempt(s): {reason}")
        else:
            notify(f"⚠️ Login catch-up: {name(plat)} for {label} failed ({reason}). Retrying in {RETRY_HOURS}h.")
    if st["pending"]:
        at = ensure_scheduled(st, datetime.now() + timedelta(hours=RETRY_HOURS))
        log(f"{len(st['pending'])} pending; next try {local(at) if at else 'not scheduled'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", nargs=2, metavar=("ROW", "PLATFORM"))
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    st = load_state()
    try:
        if args.status:
            print(json.dumps(st, indent=1))
        elif args.add:
            add(st, *args.add)
        else:
            run(st)
    finally:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
