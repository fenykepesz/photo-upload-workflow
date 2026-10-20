#!/usr/bin/env python3
"""
YouPic catch-up: upload YouPic for rows whose main run left it undone.

The free YouPic account allows one upload per 24h, and the daily run is
started from Telegram at varying times, so YouPic is regularly deferred and
the row ends Partial. This script uploads such rows to YouPic only, once the
24h slot is free.

On demand, not polling:
- When upload.py defers YouPic it calls `youpic_catchup.py --schedule-in N`,
  which creates ONE Hermes one-off job ("in Nm") for when the slot frees up.
- That run uploads the oldest waiting row and, if more rows are waiting,
  schedules the next one-off. Nothing waiting = nothing scheduled.
- A daily Hermes job runs this same script as a safety net (e.g. a one-off
  lost to bad timing). It is a no-op unless a row is actually waiting.

Candidate row: YOUPIC in platforms, url_youpic empty, status Uploaded or
Partial (its main run already happened), upload_timestamp within MAX_AGE_DAYS.

- upload.py sends the usual Telegram summary; verify_upload.py then checks
  the post, exactly like a normal run.
- The row's original upload_timestamp is restored afterwards (the dashboard's
  "last upload" and verify_upload's "latest row" key off it), and the stale
  "YouPic: deferred ..." note is removed from error_log on success.
- A row whose YouPic upload genuinely FAILS (not deferred) is retried at most
  MAX_ATTEMPTS times, then given up with one Telegram message.
- Every decision is appended to logs/youpic.log (shared with upload.py).

Usage:
  youpic_catchup.py                  do the work if due (cron entry point)
  youpic_catchup.py --schedule-in N  only make sure a one-off run exists in ~N seconds
  youpic_catchup.py --dry-run        report state and candidates; no browser
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import upload as u  # noqa: E402

LOCK_FILE = SCRIPT_DIR / ".upload.lock"
STATE_FILE = SCRIPT_DIR / "logs" / "youpic_catchup_state.json"
HERMES = "/usr/local/bin/hermes"
JOB_SCRIPT = "youpic_catchup.sh"          # under ~/.hermes/scripts/
ONEOFF_NAME = "YouPic Catch-up (one-off)"
MAX_AGE_DAYS = 14
MAX_ATTEMPTS = 3


def log(row_id, result, detail=""):
    u.youpic_history(row_id, result, detail, source="catch-up")


def notify(msg):
    """Telegram (same Photo Uploads channel as the upload summaries). Never raises."""
    try:
        u._send_telegram_message(msg, parse_mode=None)
    except Exception:
        pass


def local(dt):
    """Server time is UTC; show Erik's local (Israel) time."""
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M (%a %d %b)")


def row_label(uid, rows=None):
    rows = rows if rows is not None else u.load_queue(u.DEFAULT_CSV)
    r = next((x for x in rows if x["upload_id"] == uid), {})
    title = (r.get("title") or "").strip()
    return f'{uid} "{title}"' if title else uid


def load_state():
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    st.setdefault("next_slot", None)
    st.setdefault("attempts", {})
    st.setdefault("gave_up", [])
    st.setdefault("scheduled", None)      # {"job_id", "run_at"}
    return st


def save_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1))


def parse_ts(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime((value or "").strip(), fmt)
        except ValueError:
            pass
    return None


def candidates(rows, gave_up):
    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
    out = []
    for r in rows:
        plats = {p.strip().upper() for p in r.get("platforms", "").split(",")}
        ts = parse_ts(r.get("upload_timestamp"))
        if ("YOUPIC" in plats and not r.get("url_youpic", "").strip()
                and r.get("status", "").strip() in ("Uploaded", "Partial")
                and ts and ts >= cutoff and r["upload_id"] not in gave_up):
            out.append((ts, r))
    return [r for _, r in sorted(out, key=lambda x: x[0])]


# ── One-off scheduling (Hermes cron) ──────────────────────────
def _hermes(*args):
    return subprocess.run([HERMES, "cron", *args], capture_output=True, text=True, timeout=60)


def _job_pending(job_id):
    try:
        out = _hermes("list").stdout
    except Exception:
        return False
    i = out.find(job_id)
    return i >= 0 and "[active]" in out[i:i + 40]


def ensure_scheduled(st, when):
    """Make sure exactly one pending one-off run exists at ~`when`. Never raises.
    Returns the scheduled run time, or None if scheduling failed."""
    when = max(when, datetime.now() + timedelta(minutes=2))
    cur = st.get("scheduled") or {}
    if cur.get("job_id"):
        cur_at = datetime.fromisoformat(cur["run_at"])
        if cur_at > datetime.now() and _job_pending(cur["job_id"]):
            if abs((cur_at - when).total_seconds()) < 600:
                return cur_at                       # already scheduled for about then
            try:
                _hermes("remove", cur["job_id"])
            except Exception:
                pass
    minutes = max(1, int((when - datetime.now()).total_seconds() // 60) + 1)
    try:
        res = _hermes("create", f"in {minutes}m", "--name", ONEOFF_NAME, "--script", JOB_SCRIPT,
                      "--no-agent", "--deliver", "local", "--failure-deliver", "telegram:718581332")
        m = re.search(r"\b([0-9a-f]{12})\b", res.stdout + res.stderr)
        if res.returncode != 0 or not m:
            raise RuntimeError((res.stdout + res.stderr).strip()[-200:])
        run_at = datetime.now() + timedelta(minutes=minutes)
        st["scheduled"] = {"job_id": m.group(1), "run_at": run_at.isoformat(timespec="seconds")}
        log("-", "scheduled", f"next catch-up run at {run_at:%Y-%m-%d %H:%M} UTC (job {m.group(1)})")
        return run_at
    except Exception as e:
        st["scheduled"] = None
        log("-", "schedule FAILED", f"{e} — daily safety-net run will pick it up")
        return None


# ── Work ───────────────────────────────────────────────────────
def lock_busy():
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError):
        return False       # stale lock — run_upload.sh treats it the same way
    except PermissionError:
        return True


def youpic_slot_free():
    """Open the organizer once. Returns (free, seconds_until_free or None)."""
    from playwright.sync_api import sync_playwright
    proxy = os.environ.get("SOCKS5_PROXY", "").strip()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(u.BROWSER_PROFILE), headless=False, executable_path=u.find_chrome(),
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1280, "height": 900}, timezone_id="Asia/Jerusalem", locale="en-IL",
            **({"proxy": {"server": proxy}} if proxy else {}))
        try:
            page = ctx.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            u.apply_stealth(page)
            page.goto("https://youpic.com/organizer", wait_until="domcontentloaded", timeout=60000)
            body = ""
            for _ in range(15):
                page.wait_for_timeout(1000)
                body = page.inner_text("body")
                if "Upload Media" in body or "Next Upload" in body:
                    break
        finally:
            u.close_context_gracefully(ctx)
    if "Next Upload" in body:
        return False, u.youpic_countdown_secs(body)
    if "Upload Media" in body:
        return True, None
    raise RuntimeError("YouPic organizer showed neither the upload area nor the countdown (logged out?)")


def run(st):
    rows = u.load_queue(u.DEFAULT_CSV)
    todo = candidates(rows, set(st["gave_up"]))
    if not todo:
        return
    uid = todo[0]["upload_id"]
    if st["next_slot"] and datetime.now() < datetime.fromisoformat(st["next_slot"]):
        ensure_scheduled(st, datetime.fromisoformat(st["next_slot"]))
        return
    if lock_busy():
        log(uid, "waiting", "browser profile busy (another upload running) — retry in 15 min")
        ensure_scheduled(st, datetime.now() + timedelta(minutes=15))
        return

    row = todo[0]
    LOCK_FILE.write_text(str(os.getpid()))
    try:
        try:
            free, secs = youpic_slot_free()
        except Exception as e:
            log(uid, "slot check FAILED", str(e)[:200])
            last = st.get("last_alert")
            if not last or datetime.now() - datetime.fromisoformat(last) > timedelta(hours=24):
                st["last_alert"] = datetime.now().isoformat(timespec="seconds")
                u._send_telegram_message(
                    f"⚠️ YouPic catch-up couldn't check the upload slot ({str(e)[:150]}). "
                    f"{uid} is still waiting for YouPic. If YouPic logged out: python upload.py --login",
                    parse_mode=None)
            ensure_scheduled(st, datetime.now() + timedelta(hours=6))
            return
        if not free:
            st["next_slot"] = (datetime.now() + timedelta(seconds=secs + 120)).isoformat(timespec="seconds")
            log(uid, "waiting", f"24h cap active, slot frees ~{st['next_slot']}")
            at = ensure_scheduled(st, datetime.fromisoformat(st["next_slot"]))
            notify(f"⏳ YouPic catch-up: {row_label(uid, rows)} — the 24h limit is still active. "
                   + (f"Next try at {local(at)}." if at else "The daily safety-net run will try again."))
            return

        log(uid, "starting", f"slot free — uploading ({len(todo)} row(s) waiting)")
        orig_ts = row.get("upload_timestamp", "")
        raw = SCRIPT_DIR / "logs" / f"raw_youpic_catchup_{datetime.now():%Y%m%d_%H%M%S}.log"
        env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"), YOUPIC_CATCHUP="1")
        with open(raw, "w") as fh:
            rc = subprocess.call([sys.executable, "upload.py", "--row", uid, "--platform", "YOUPIC",
                                  "--skip-login-check"], cwd=SCRIPT_DIR, stdout=fh, stderr=subprocess.STDOUT, env=env)
        out = raw.read_text(errors="replace")
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass

    fresh = next((r for r in u.load_queue(u.DEFAULT_CSV) if r["upload_id"] == uid), {})
    url = fresh.get("url_youpic", "").strip()
    fix = {"upload_timestamp": orig_ts}
    remaining = len(todo) - 1
    if url:
        st["attempts"].pop(uid, None)
        st["next_slot"] = (datetime.now() + timedelta(hours=24, minutes=2)).isoformat(timespec="seconds")
        kept = [e for e in fresh.get("error_log", "").split("; ") if e and not e.startswith("YouPic:")]
        fix["error_log"] = "; ".join(kept)
        subprocess.Popen([sys.executable, "verify_upload.py", "--row", uid, "--platforms", "YOUPIC",
                          "--delay", "60", "--recheck", "180"], cwd=SCRIPT_DIR, env=env,
                         stdout=open(SCRIPT_DIR / "logs" / "verify_run.log", "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        at = ensure_scheduled(st, datetime.fromisoformat(st["next_slot"])) if remaining else None
        lines = [f"✅ YouPic catch-up done: {row_label(uid, rows)} is on YouPic.", url,
                 f"Row status: {fresh.get('status', '?')}."]
        if remaining:
            lines.append(f"{remaining} more photo(s) waiting for YouPic"
                         + (f" — next upload at {local(at)}." if at else "."))
        notify("\n".join(lines))
    elif "deferred" in out.lower():
        st["next_slot"] = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        at = ensure_scheduled(st, datetime.fromisoformat(st["next_slot"]))
        notify(f"⏳ YouPic catch-up: {row_label(uid, rows)} — YouPic still reported the 24h limit. "
               + (f"Next try at {local(at)}." if at else "The daily safety-net run will try again."))
    else:
        n = st["attempts"].get(uid, 0) + 1
        st["attempts"][uid] = n
        log(uid, "attempt failed", f"{n}/{MAX_ATTEMPTS}, upload.py rc={rc}, see logs/{raw.name}")
        m = re.search(r"YouPic: FAILED \([^)]*\) — (.+)", out)
        reason = m.group(1).strip()[:200] if m else f"see logs/{raw.name}"
        if n >= MAX_ATTEMPTS:
            st["gave_up"].append(uid)
            st["attempts"].pop(uid, None)
            log(uid, "GAVE UP", f"after {MAX_ATTEMPTS} failed attempts")
            u._send_telegram_message(
                f"⚠️ YouPic catch-up gave up on {uid} after {MAX_ATTEMPTS} failed attempts. "
                f"See logs/youpic.log and logs/{raw.name}; use Retry in the queue manager once fixed.",
                parse_mode=None)
            if remaining:
                ensure_scheduled(st, datetime.now() + timedelta(hours=2))
        else:
            at = ensure_scheduled(st, datetime.now() + timedelta(hours=2))
            notify(f"⚠️ YouPic catch-up: {row_label(uid, rows)} failed (attempt {n}/{MAX_ATTEMPTS}): {reason}. "
                   + (f"Retrying at {local(at)}." if at else "The daily safety-net run will retry."))
    u.save_row_update(u.DEFAULT_CSV, uid, fix)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--schedule-in", type=int, metavar="SECONDS")
    ap.add_argument("--row", default="", help="with --schedule-in: the row just deferred (for the Telegram notice)")
    args = ap.parse_args()

    st = load_state()
    if args.dry_run:
        rows = u.load_queue(u.DEFAULT_CSV)
        print(json.dumps({k: st[k] for k in ("next_slot", "attempts", "gave_up", "scheduled")}, indent=1))
        print("candidates (oldest first):", [r["upload_id"] for r in candidates(rows, set(st["gave_up"]))] or "none")
        return 0
    if args.schedule_in is not None:
        when = datetime.now() + timedelta(seconds=args.schedule_in)
        st["next_slot"] = when.isoformat(timespec="seconds")
        at = ensure_scheduled(st, when)
        save_state(st)
        if args.row:
            notify(f"⏳ YouPic deferred: {row_label(args.row)} — the free account allows one upload per 24h. "
                   + (f"Catch-up scheduled for {local(at)}; you'll get a message when it's done."
                      if at else "Couldn't schedule the catch-up — the daily safety-net run will pick it up."))
        return 0
    try:
        run(st)
    finally:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
