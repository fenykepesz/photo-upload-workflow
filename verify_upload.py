#!/usr/bin/env python3
"""
Post-upload verification — READ-ONLY.

upload.py reports SUCCESS for 35photo, X, 500px, the VK wall and Facebook
after clicking the publish button and waiting a few seconds; nothing confirms
the post actually exists. This script runs separately, a couple of minutes
after an upload, opens the logged-in profile pages and looks for the post.

It only navigates and reads. It never clicks, types, or writes to the queue,
and it cannot trigger a retry — a miss is reported to Telegram, nothing more.

Usage:
  verify_upload.py                      verify the most recently uploaded row
  verify_upload.py --row PH-2026-212    verify a specific row
  verify_upload.py --since "2026-10-05 20:00:00"   every row uploaded since then
  --delay N      wait N seconds before the first check (default 0)
  --recheck N    wait N seconds, then re-check anything not found (default 180, 0 = off)
  --platforms    comma list to limit the check, e.g. FB,VK
  --no-telegram  print only
"""

import argparse
import html
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import upload as u
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).parent.resolve()
LOCK_FILE = SCRIPT_DIR / ".upload.lock"   # shared with run_upload.sh
LOG_FILE = SCRIPT_DIR / "logs" / "verify.log"

# Platform -> queue column upload.py fills in when it reports success.
URL_FIELD = {"35P": "url_35p", "X": "url_x", "500PX": "url_500px", "VK": "url_vk", "FB": "url_fb"}
ORDER = ["500PX", "35P", "VK", "X", "FB"]
LABEL = {"35P": "35photo", "X": "X", "500PX": "500px", "VK": "VK", "FB": "Facebook"}

OK, MISSING, UNKNOWN = "found", "NOT FOUND", "could not check"

HREFS_JS = ("(pat) => [...new Set(Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href).filter(h => new RegExp(pat).test(h)))]")

# Page text plus alt/title attributes — 500px and 35photo carry the title in
# attributes of the thumbnail, not always in visible text.
TEXT_JS = """() => {
  const attrs = Array.from(document.querySelectorAll('[alt],[title]'))
      .map(e => (e.getAttribute('alt') || '') + ' ' + (e.getAttribute('title') || ''));
  return (document.body ? document.body.innerText : '') + ' ' + attrs.join(' ');
}"""

# Closest link (matching pat) to the first element that carries the needle.
LINK_NEAR_JS = """([needle, pat]) => {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9\\u0400-\\u04ff]+/g, ' ').trim();
  const re = new RegExp(pat);
  const els = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) if (norm(n.nodeValue).includes(needle) && n.parentElement) els.push(n.parentElement);
  for (const e of document.querySelectorAll('[alt],[title]'))
    if (norm(e.getAttribute('alt')).includes(needle) || norm(e.getAttribute('title')).includes(needle)) els.push(e);
  for (const el of els.slice(0, 5)) {
    let a = el;
    for (let i = 0; i < 10 && a; i++, a = a.parentElement) {
      const hs = [a.tagName === 'A' ? a : null, ...a.querySelectorAll('a[href]')].filter(Boolean)
          .map(x => x.href).filter(h => re.test(h));
      if (hs.length) return hs[0];
    }
  }
  return '';
}"""


def norm(s):
    """Lowercase, punctuation and whitespace collapsed — so dashes, smart
    quotes and line wraps on the page don't break a match."""
    return re.sub(r"[^a-z0-9Ѐ-ӿ]+", " ", (s or "").lower()).strip()


def caption_needle(row, words=8):
    """First few words of the caption. VK and Facebook posts don't contain
    the title, only credit lines + caption."""
    return " ".join(norm(row.get("caption", "")).split()[:words])


def page_has(page, needle):
    return bool(needle) and needle in norm(page.evaluate(TEXT_JS))


def logged_out(page):
    url = page.url.lower()
    return any(s in url for s in ("/login", "/i/flow/login", "checkpoint", "/signin", "act=login"))


# ── Per-platform checks ───────────────────────────────────────
# Each returns (status, detail). detail is the post URL when found.

def _check_title_on_profile(page, profile_url, needle, link_pat):
    if not needle:
        return UNKNOWN, "row has no title"
    page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(6000)
    if logged_out(page):
        return UNKNOWN, f"redirected to login ({page.url[:60]})"
    for _ in range(3):
        if page_has(page, needle):
            return OK, page.evaluate(LINK_NEAR_JS, [needle, link_pat]) or profile_url
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(2000)
    return MISSING, f"title not on {profile_url}"


def check_35p(page, row, accounts):
    return _check_title_on_profile(
        page, f"https://35photo.pro/{accounts['35photo']}/", norm(row.get("title")), r"photo_\d+")


# The tweet whose text carries the needle: its link and how many photos it has.
X_TWEET_JS = """(needle) => {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9\\u0400-\\u04ff]+/g, ' ').trim();
  for (const a of document.querySelectorAll('article')) {
    if (!norm(a.innerText).includes(needle)) continue;
    const link = Array.from(a.querySelectorAll('a[href]')).map(x => x.href).find(h => /\\/status\\/\\d+$/.test(h)) || '';
    return {link, photos: a.querySelectorAll('[data-testid="tweetPhoto"] img').length};
  }
  return null;
}"""


def check_x(page, row, accounts):
    needle = norm(row.get("title"))
    if not needle:
        return UNKNOWN, "row has no title"
    profile_url = f"https://x.com/{accounts['x']}"
    page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(6000)
    if logged_out(page):
        return UNKNOWN, f"redirected to login ({page.url[:60]})"
    for _ in range(3):
        tweet = page.evaluate(X_TWEET_JS, needle)
        if tweet:
            # X can publish the text and silently drop the attachment.
            if not tweet["photos"]:
                return MISSING, f"post {tweet['link'] or profile_url} has the text but no photo"
            return OK, tweet["link"] or profile_url
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(2000)
    return MISSING, f"title not on {profile_url}"


def check_500px(page, row, accounts):
    return _check_title_on_profile(
        page, f"https://500px.com/p/{accounts['500px']}", norm(row.get("title")), r"/photo/")


def check_vk(page, row, accounts):
    needle = caption_needle(row)
    if not needle:
        return UNKNOWN, "row has no caption to match on"
    page.goto(f"https://vk.com/{accounts['vk']}", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)
    if logged_out(page):
        return UNKNOWN, f"redirected to login ({page.url[:60]})"
    links = []
    for _ in range(4):
        links = page.evaluate(HREFS_JS, r"/wall\d+_\d+$")
        if links:
            break
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2500)
    if not links:
        return UNKNOWN, "no wall posts visible on profile"
    owner = re.search(r"wall(\d+)_", links[0]).group(1)
    ids = sorted({int(re.search(r"_(\d+)$", h).group(1)) for h in links}, reverse=True)
    for pid in ids[:3]:
        page.goto(f"https://vk.com/wall{owner}_{pid}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        if page_has(page, needle):
            imgs = page.evaluate(
                "() => Array.from(document.querySelectorAll('img')).filter(i => i.naturalWidth > 300).length")
            if not imgs:
                return MISSING, f"post {page.url} has the caption but no photo"
            return OK, page.url
    return MISSING, f"caption not in the 3 newest wall posts (newest: {ids[0]})"


def check_fb(page, row, accounts):
    needle = caption_needle(row)
    if not needle:
        return UNKNOWN, "row has no caption to match on"
    # The feed truncates captions and unloads posts while scrolling; the
    # Photos tab is stable and newest-first.
    page.goto(f"https://www.facebook.com/{accounts['facebook']}/photos",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(7000)
    if logged_out(page):
        return UNKNOWN, f"redirected to login ({page.url[:60]})"
    fbids = []
    for h in page.evaluate(HREFS_JS, r"fbid=\d+&set=pb\."):
        m = re.search(r"fbid=(\d+)", h)
        if m and m.group(1) not in fbids:
            fbids.append(m.group(1))
    if not fbids:
        return UNKNOWN, "no photos visible on the Photos tab"
    for fbid in fbids[:3]:
        url = f"https://www.facebook.com/photo/?fbid={fbid}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)
        if page_has(page, needle):
            return OK, url
    return MISSING, "caption not on the 3 newest photos"


CHECKS = {"35P": check_35p, "X": check_x, "500PX": check_500px, "VK": check_vk, "FB": check_fb}


# ── Row selection ─────────────────────────────────────────────

def select_rows(rows, args):
    done = [r for r in rows if r.get("status") in ("Uploaded", "Partial") and r.get("upload_timestamp", "").strip()]
    if args.row:
        hit = [r for r in rows if r.get("upload_id") == args.row]
        if not hit:
            sys.exit(f"ERROR: row {args.row} not found in queue")
        return hit
    if args.since:
        return [r for r in done if r["upload_timestamp"] >= args.since]
    return [max(done, key=lambda r: r["upload_timestamp"])] if done else []


def platforms_to_check(row, only):
    """Only platforms upload.py reported as uploaded — a platform it already
    marked failed has been reported once and isn't this script's job."""
    wanted = {p.strip().upper() for p in row.get("platforms", "").split(",")}
    out, skipped = [], []
    for p in ORDER:
        if p not in wanted or (only and p not in only):
            continue
        (out if row.get(URL_FIELD[p], "").strip() else skipped).append(p)
    return out, skipped


def ambiguous_title(row, rows):
    """Another uploaded row whose title contains this one would also satisfy
    the title match on 35photo/X/500px."""
    t = norm(row.get("title"))
    return [r["upload_id"] for r in rows
            if r is not row and t and t in norm(r.get("title")) and r.get("status") in ("Uploaded", "Partial")]


# ── Profile / lock handling ───────────────────────────────────

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, TypeError):
        return False


def acquire_lock(max_wait):
    """Take run_upload.sh's lock so an upload can't start on the same browser
    profile mid-check. Waits if an upload is running right now."""
    deadline = time.time() + max_wait
    while True:
        holder = LOCK_FILE.read_text().strip() if LOCK_FILE.exists() else ""
        if not holder or not _pid_alive(holder) or holder == str(os.getpid()):
            LOCK_FILE.write_text(str(os.getpid()))
            return True
        if time.time() > deadline:
            return False
        print(f"  Upload running (PID {holder}) — waiting...")
        time.sleep(30)


def release_lock():
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def log(line):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


# ── Main ──────────────────────────────────────────────────────

def run_pass(page, todo, accounts, results, pass_no):
    """todo: list of (row, platform). Updates results[(upload_id, platform)]."""
    for row, p in todo:
        t0 = time.time()
        try:
            status, detail = CHECKS[p](page, row, accounts)
        except Exception as e:
            status, detail = UNKNOWN, f"{type(e).__name__}: {str(e)[:120]}"
        results[(row["upload_id"], p)] = (status, detail, pass_no)
        print(f"  {row['upload_id']} {LABEL[p]:9} {status} ({time.time()-t0:.0f}s) — {detail}")


def main():
    ap = argparse.ArgumentParser(description="Read-only check that uploaded posts actually exist.")
    ap.add_argument("--row")
    ap.add_argument("--since")
    ap.add_argument("--delay", type=int, default=0)
    ap.add_argument("--recheck", type=int, default=180)
    ap.add_argument("--platforms", default="")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--csv", type=Path, default=u.DEFAULT_CSV)
    ap.add_argument("--config", type=Path, default=u.DEFAULT_CONFIG)
    ap.add_argument("--profile", type=Path, default=u.BROWSER_PROFILE)
    args = ap.parse_args()
    only = {p.strip().upper() for p in args.platforms.split(",") if p.strip()}

    if args.delay:
        print(f"Waiting {args.delay}s before checking...")
        time.sleep(args.delay)

    all_rows = u.load_queue(args.csv)
    accounts = u.load_config(args.config).get("accounts", {})
    rows = select_rows(all_rows, args)
    todo, notes = [], []
    for row in rows:
        plats, skipped = platforms_to_check(row, only)
        todo += [(row, p) for p in plats]
        if skipped:
            notes.append(f"{row['upload_id']}: {', '.join(LABEL[p] for p in skipped)} not checked (upload already reported failed)")
        dup = ambiguous_title(row, all_rows)
        if dup and any(p in ("35P", "X", "500PX") for p in plats):
            notes.append(f"{row['upload_id']}: title also matches {', '.join(dup[:3])} — title checks are less certain")
    if not todo:
        print("Nothing to verify.")
        return 0

    if not acquire_lock(max_wait=30 * 60):
        msg = "⚠️ Upload verification skipped — an upload was still running after 30 min."
        print(msg)
        if not args.no_telegram:
            u._send_telegram_message(msg, parse_mode=None)
        return 2

    results = {}
    started = time.time()
    try:
        proxy = os.environ.get("SOCKS5_PROXY", "").strip()
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(args.profile),
                headless=False,
                executable_path=u.find_chrome(),
                args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                      "--no-default-browser-check"],
                viewport={"width": 1280, "height": 900},
                timezone_id="Asia/Jerusalem",
                locale="en-IL",
                **({"proxy": {"server": proxy}} if proxy else {}),
            )
            try:
                page = ctx.new_page()
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                u.apply_stealth(page)
                print("── Pass 1 ──")
                run_pass(page, todo, accounts, results, 1)
                again = [(r, p) for r, p in todo if results[(r["upload_id"], p)][0] != OK]
                if again and args.recheck:
                    print(f"── {len(again)} not confirmed — re-checking in {args.recheck}s ──")
                    page.goto("about:blank")
                    time.sleep(args.recheck)
                    run_pass(page, again, accounts, results, 2)
            finally:
                u.close_context_gracefully(ctx)
    finally:
        release_lock()

    # ── Report ──
    for (uid, p), (status, detail, pass_no) in results.items():
        log(f"{uid}  {p:<6} {status:<16} pass={pass_no}  {detail}")
    bad = {k: v for k, v in results.items() if v[0] != OK}
    n_ok = len(results) - len(bad)
    if not bad:
        header = f"✅ Verified. All {len(results)} posts found on the live profiles."
    else:
        header = f"⚠️ Verification: {n_ok}/{len(results)} posts found. Nothing was retried."
    parts = [header]
    for row in rows:
        mine = [(p, v) for (uid, p), v in results.items() if uid == row["upload_id"]]
        if not mine:
            continue
        table = [f"{'Platform':<9} Result"]
        for p, (status, _detail, pass_no) in mine:
            cell = {OK: "✓ found", MISSING: "✗ NOT FOUND", UNKNOWN: "? not checked"}[status]
            if status == OK and pass_no == 2:
                cell += " (re-check)"
            table.append(f"{LABEL[p]:<9} {cell}")
        parts.append(f"\n📸 <b>{html.escape(row.get('title') or row['upload_id'])}</b>  🆔 {row['upload_id']}"
                     f"\n<pre>" + "\n".join(table) + "</pre>")
        for p, (status, detail, _) in mine:
            if status != OK:
                parts.append(f"{LABEL[p]}: {html.escape(detail)}")
    if bad:
        parts.append("Check the profile before re-uploading.")
    parts += [html.escape(n) for n in notes]
    msg = "\n".join(parts)
    print(f"\n{msg}\n(total {time.time()-started:.0f}s)")
    if not args.no_telegram:
        u._send_telegram_message(msg)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
