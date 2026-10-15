#!/usr/bin/env python3
"""
YouPic catch-up: upload YouPic for rows whose main run left it undone.

The free YouPic account allows one upload per 24h. The daily run is started
from Telegram at varying times, so YouPic is regularly deferred ("Next
Upload" countdown still running) and the row ends Partial. This script runs
on its own Hermes cron schedule (every 2h), and when the 24h slot is free it
uploads the OLDEST such row to YouPic only, via upload.py.

Candidate row: YOUPIC in platforms, url_youpic empty, status Uploaded or
Partial (its main run already happened), upload_timestamp within MAX_AGE_DAYS.

- Quiet no-op (exit 0, no output) when there's nothing to do, the browser
  profile is busy (.upload.lock), or the countdown is still running. The
  countdown end is remembered in a state file so the browser isn't opened
  again before then.
- upload.py sends the usual Telegram summary; verify_upload.py then checks
  the post, exactly like a normal run.
- The row's original upload_timestamp is restored afterwards (the dashboard's
  "last upload" and verify_upload's "latest row" key off it), and the stale
  "YouPic: deferred ..." note is removed from error_log on success.
- A row whose YouPic upload genuinely FAILS (not deferred) is retried at most
  MAX_ATTEMPTS times, then given up with one Telegram message.

Usage: youpic_catchup.py [--dry-run]   (--dry-run: report what it would do; no browser)
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

LOCK_FILE = SCRIPT_DIR / ".upload.lock"
STATE_FILE = SCRIPT_DIR / "logs" / "youpic_catchup_state.json"
LOG_FILE = SCRIPT_DIR / "logs" / "youpic_catchup.log"
MAX_AGE_DAYS = 14
MAX_ATTEMPTS = 3


def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"next_slot": None, "attempts": {}, "gave_up": []}


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
        m = re.search(r"Next Upload\s*(\d{1,2})\s*:\s*(\d{2})", body)
        secs = (int(m.group(1)) * 3600 + int(m.group(2)) * 60) if m else 3600
        return False, secs
    if "Upload Media" in body:
        return True, None
    raise RuntimeError("YouPic organizer showed neither the upload area nor the countdown (logged out?)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    st = load_state()
    rows = u.load_queue(u.DEFAULT_CSV)
    todo = candidates(rows, set(st.get("gave_up", [])))
    if args.dry_run:
        print(f"next_slot={st.get('next_slot')}  attempts={st.get('attempts')}  gave_up={st.get('gave_up')}")
        print("candidates (oldest first):", [r["upload_id"] for r in todo] or "none")
        return 0
    if not todo:
        return 0
    if st.get("next_slot") and datetime.now() < datetime.fromisoformat(st["next_slot"]):
        return 0
    if lock_busy():
        log("browser profile busy — skipped")
        return 0

    row = todo[0]
    uid = row["upload_id"]
    LOCK_FILE.write_text(str(os.getpid()))
    try:
        try:
            free, secs = youpic_slot_free()
        except Exception as e:
            log(f"slot check failed: {e}")
            last = st.get("last_alert")
            if not last or datetime.now() - datetime.fromisoformat(last) > timedelta(hours=24):
                st["last_alert"] = datetime.now().isoformat(timespec="seconds")
                save_state(st)
                u._send_telegram_message(
                    f"⚠️ YouPic catch-up couldn't check the upload slot ({str(e)[:150]}). "
                    f"{uid} is still waiting for YouPic. If YouPic logged out: python upload.py --login",
                    parse_mode=None)
            return 0
        if not free:
            st["next_slot"] = (datetime.now() + timedelta(seconds=secs + 120)).isoformat(timespec="seconds")
            save_state(st)
            log(f"{uid}: 24h cap active, next slot ~{st['next_slot']}")
            return 0

        log(f"{uid}: slot free — uploading to YouPic")
        orig_ts = row.get("upload_timestamp", "")
        raw = SCRIPT_DIR / "logs" / f"raw_youpic_catchup_{datetime.now():%Y%m%d_%H%M%S}.log"
        env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
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
    if url:
        st["attempts"].pop(uid, None)
        st["next_slot"] = (datetime.now() + timedelta(hours=24, minutes=2)).isoformat(timespec="seconds")
        kept = [e for e in fresh.get("error_log", "").split("; ") if e and not e.startswith("YouPic:")]
        fix["error_log"] = "; ".join(kept)
        log(f"{uid}: YouPic OK {url} (upload.py rc={rc})")
        subprocess.Popen([sys.executable, "verify_upload.py", "--row", uid, "--platforms", "YOUPIC",
                          "--delay", "60", "--recheck", "180"], cwd=SCRIPT_DIR, env=env,
                         stdout=open(SCRIPT_DIR / "logs" / "verify_run.log", "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    elif "deferred" in out.lower():
        st["next_slot"] = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        log(f"{uid}: deferred by YouPic after all — retry after {st['next_slot']}")
    else:
        n = st["attempts"].get(uid, 0) + 1
        st["attempts"][uid] = n
        log(f"{uid}: YouPic FAILED (attempt {n}/{MAX_ATTEMPTS}, rc={rc}) — see {raw.name}")
        if n >= MAX_ATTEMPTS:
            st.setdefault("gave_up", []).append(uid)
            st["attempts"].pop(uid, None)
            u._send_telegram_message(
                f"⚠️ YouPic catch-up gave up on {uid} after {MAX_ATTEMPTS} failed attempts. "
                f"Check logs/{raw.name}; use Retry in the queue manager once fixed.", parse_mode=None)
    u.save_row_update(u.DEFAULT_CSV, uid, fix)
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
