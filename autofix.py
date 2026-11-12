#!/usr/bin/env python3
"""Upload doctor — diagnose failed uploads with Claude Code, propose a fix,
apply it only when Erik approves ("fix N" on Telegram → Hermes runs `apply N`).

  autofix.py trigger --since "YYYY-mm-dd HH:MM:SS" [--missing-json JSON]
      Called by verify_upload.py after a scheduled run. Collects failures
      (run log ROW_DONE ":failed" + verify NOT FOUND), filters out things code
      can't fix, and starts a background diagnosis for the rest.
  autofix.py diagnose N        (internal — the background diagnosis)
  autofix.py apply N [--dry] [--background]
      Deterministic, no AI: check live code unchanged, back up, apply patch,
      syntax checks, commit + push, retry that one platform for that row.
  autofix.py skip N            discard a proposal
  autofix.py status            list recent incidents
  autofix.py test --row R --platform P --error TEXT
      Fake incident: real diagnosis, but apply is forced to --dry.

Safety: the diagnosis session works on a separate git worktree in
/root/autofix/incidents/N/repo — the live code is only changed by `apply`.
Kill switch: touch /root/autofix/OFF (trigger does nothing while it exists).
Added 2026-11-12 (Erik: option B — diagnose + prepare fix, apply on "go").
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import upload as u  # noqa: E402

ROOT = Path("/root/autofix")
INC = ROOT / "incidents"
STATE = ROOT / "state.json"
OFF = ROOT / "OFF"
CLAUDE = "/root/.local/bin/claude"
PY = str(SCRIPT_DIR / "venv" / "bin" / "python")
LOCK = SCRIPT_DIR / ".upload.lock"
BACKUPS = Path("/root/.hermes-manual-backups")
LOGS = SCRIPT_DIR / "logs"

MAX_SESSIONS_PER_DAY = 3
MAX_TRIES_PER_KEY = 2          # a 2nd diagnosis only if the 1st fix didn't help
EXPIRE_HOURS = 48
SESSION_TIMEOUT = 30 * 60
UPLOAD_TIMEOUT = 40 * 60

URL_FIELD = {"DA": "da_deviation_url", "500PX": "url_500px", "35P": "url_35p", "VK": "url_vk",
             "X": "url_x", "BSKY": "url_bsky", "FB": "url_fb", "IG": "url_ig", "YOUPIC": "url_youpic"}
# Labels used in ROW_DONE lines and in the raw log's "  <Label>: FAILED" prints.
ALIASES = {"500PX": ["500px"], "35P": ["35photo"], "YOUPIC": ["YouPic"], "VK": ["VK"], "X": ["X"],
           "BSKY": ["BSKY", "Bluesky"], "FB": ["FB", "Facebook"], "IG": ["IG", "Instagram"],
           "DA": ["DA", "DeviantArt"]}
LABEL_TO_CODE = {a: c for c, al in ALIASES.items() for a in al}
NAME = u.PLATFORM_NAMES

# Failures where a code change isn't the answer. (regex on the error text, kind, explanation)
TRIAGE = [
    (r"daily upload limit|no publication slots", "limit", "35photo's daily limit was reached — not a code problem."),
    (r"stash_url_safe", "data", "the row has no safe (non-NSFW) version for Instagram — a queue data issue."),
    (r"net::ERR_|ERR_TIMED_OUT|ERR_CONNECTION|got no answer|connection dropped|profile unreachable",
     "network", "the site didn't answer (network drop) — usually fine on a retry."),
]


# ── small helpers ────────────────────────────────────────────
def now():
    return datetime.now()


def ts():
    return now().strftime("%Y%m%d_%H%M%S")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"next_id": 1}


def save_state(st):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def inc_dir(n):
    return INC / str(n)


def load_inc(n):
    p = inc_dir(n) / "incident.json"
    if not p.exists():
        sys.exit(f"No incident {n}.")
    return json.loads(p.read_text())


def save_inc(inc):
    d = inc_dir(inc["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "incident.json.tmp"
    tmp.write_text(json.dumps(inc, indent=1))
    tmp.replace(d / "incident.json")


def all_incs():
    out = []
    if INC.exists():
        for d in INC.iterdir():
            f = d / "incident.json"
            if f.exists():
                try:
                    out.append(json.loads(f.read_text()))
                except Exception:
                    pass
    return sorted(out, key=lambda i: i["id"])


def home_chat():
    try:
        for line in Path("/root/.hermes/.env").read_text().splitlines():
            if line.startswith("TELEGRAM_HOME_CHANNEL="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def notify(text):
    """To Erik's Hermes chat (so a reply reaches Hermes). Never raises."""
    print(text)
    try:
        u._send_telegram_message(text, parse_mode=None, chat_id=home_chat())
    except Exception as e:
        print(f"  (telegram failed: {e})")


def md5(path):
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def git(*args, cwd=SCRIPT_DIR, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def row_title(row_id):
    try:
        r = next((x for x in u.load_queue(u.DEFAULT_CSV) if x["upload_id"] == row_id), {})
        return (r.get("title") or "").strip()
    except Exception:
        return ""


def label(inc):
    t = f' "{inc["title"]}"' if inc.get("title") else ""
    return f'{NAME.get(inc["platform"], inc["platform"])} for {inc["row"]}{t}'


def lock_holder():
    try:
        pid = int(LOCK.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def acquire_lock(max_wait):
    deadline = time.time() + max_wait
    while True:
        if lock_holder() is None:
            LOCK.write_text(str(os.getpid()))
            time.sleep(0.5)
            if LOCK.read_text().strip() == str(os.getpid()):
                return True
        if time.time() > deadline:
            return False
        time.sleep(15)


def release_lock():
    try:
        if LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except Exception:
        pass


def detach(args, logfile):
    """Start `autofix.py <args>` fully detached (survives the caller exiting)."""
    with open(logfile, "a") as fh:
        subprocess.Popen([PY, str(SCRIPT_DIR / "autofix.py"), *args], cwd=SCRIPT_DIR,
                         stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ, "DISPLAY": ":99"})


def cleanup_worktree(inc):
    repo = inc_dir(inc["id"]) / "repo"
    if repo.exists():
        git("worktree", "remove", "--force", str(repo), check=False)
    git("worktree", "prune", check=False)
    git("branch", "-D", f"autofix/{inc['id']}", check=False)


def expire_old():
    for inc in all_incs():
        if inc["status"] in ("ready", "retry_only", "diagnosing") and \
                now() - datetime.fromisoformat(inc["created"]) > timedelta(hours=EXPIRE_HOURS):
            inc["status"] = "expired"
            save_inc(inc)
            cleanup_worktree(inc)


# ── collecting failures ──────────────────────────────────────
def run_logs_since(since):
    out = []
    for f in sorted(LOGS.glob("run_*.log")):
        m = re.match(r"run_(\d{8}_\d{6})\.log$", f.name)
        if m and datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") >= since - timedelta(seconds=10):
            out.append(f)
    return out


def raw_error(raw, code):
    """Last '  <Label>: FAILED ... — reason' line for this platform in the raw log."""
    if not raw or not raw.exists():
        return ""
    names = "|".join(re.escape(a) for a in ALIASES.get(code, [code]))
    last = ""
    for line in raw.read_text(errors="replace").splitlines():
        m = re.match(rf"^\s+(?:{names}): FAILED[^—]*—\s*(.+)$", line)
        if m:
            last = m.group(1).strip()
    return last


def collect(since, missing):
    found = []
    for rl in run_logs_since(since):
        raw = rl.with_name(rl.name.replace("run_", "raw_run_", 1))
        for line in rl.read_text(errors="replace").splitlines():
            m = re.search(r"ROW_DONE\s+PID=\S+\s+(\S+):\s+(.*)$", line)
            if not m:
                continue
            for part in m.group(2).split(", "):
                lab, _, status = part.partition(":")
                code = LABEL_TO_CODE.get(lab.strip())
                if code and status.strip() == "failed":
                    found.append({"row": m.group(1), "platform": code, "source": "run",
                                  "error": raw_error(raw, code) or "FAILED (no reason printed)",
                                  "run_log": str(rl), "raw_log": str(raw) if raw.exists() else ""})
    for mrow in missing or []:
        found.append({"row": mrow["row"], "platform": mrow["platform"], "source": "verify",
                      "error": "upload reported success but verify did NOT find the post: " + mrow.get("detail", ""),
                      "run_log": "", "raw_log": ""})
        rls = run_logs_since(since)
        if rls:
            found[-1]["run_log"] = str(rls[-1])
            raw = rls[-1].with_name(rls[-1].name.replace("run_", "raw_run_", 1))
            found[-1]["raw_log"] = str(raw) if raw.exists() else ""
    # de-dup within this batch
    seen, uniq = set(), []
    for f in found:
        k = (f["row"], f["platform"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def screenshots_for(row, since):
    shots = []
    for d in (SCRIPT_DIR, LOGS):
        for p in d.glob("*.png"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) >= since - timedelta(minutes=1) and \
                        (row in p.name or d == LOGS):
                    shots.append(str(p))
            except Exception:
                pass
    return sorted(shots)[-15:]


def new_incident(st, f, since, status, test=False):
    n = st["next_id"]
    st["next_id"] = n + 1
    inc = {"id": n, "created": now().isoformat(timespec="seconds"), "test": test,
           "since": since.strftime("%Y-%m-%d %H:%M:%S"), "title": row_title(f["row"]),
           "status": status, "screenshots": screenshots_for(f["row"], since), **f}
    save_inc(inc)
    return inc


def cmd_trigger(args):
    if OFF.exists():
        print("autofix is switched off (/root/autofix/OFF).")
        return 0
    expire_old()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S")
    missing = json.loads(args.missing_json) if args.missing_json else []
    failures = collect(since, missing)
    if not failures:
        print("No failures to look at.")
        return 0
    st = load_state()
    prior = all_incs()
    today = now().date().isoformat()
    sessions_today = sum(1 for i in prior if i["created"][:10] == today and i.get("session"))
    lines = []
    for f in failures:
        key_incs = [i for i in prior if i["row"] == f["row"] and i["platform"] == f["platform"] and not i.get("test")]
        if any(i["status"] in ("diagnosing", "ready", "retry_only") for i in key_incs):
            print(f"{f['row']} {f['platform']}: already being handled")
            continue
        if len(key_incs) >= MAX_TRIES_PER_KEY:
            lines.append(f"• {NAME.get(f['platform'])} for {f['row']} failed again ({f['error'][:120]}). "
                         f"Already tried {len(key_incs)}× — leaving it for you/Claude.")
            continue
        kind = next(((k, why) for rx, k, why in TRIAGE if re.search(rx, f["error"], re.I)), None)
        if kind:
            k, why = kind
            inc = new_incident(st, f, since, "retry_only" if k == "network" else "no_fix")
            inc["kind"] = k
            save_inc(inc)
            msg = f"• #{inc['id']} {label(inc)} — {why}"
            if k == "network":
                msg += f"\n  Reply \"fix {inc['id']}\" to retry it now, or \"skip {inc['id']}\"."
            lines.append(msg)
            continue
        if sessions_today >= MAX_SESSIONS_PER_DAY:
            inc = new_incident(st, f, since, "no_fix")
            lines.append(f"• #{inc['id']} {label(inc)} failed ({f['error'][:150]}). "
                         f"Daily limit of {MAX_SESSIONS_PER_DAY} diagnoses reached — not diagnosed.")
            continue
        inc = new_incident(st, f, since, "diagnosing")
        sessions_today += 1
        detach(["diagnose", str(inc["id"])], inc_dir(inc["id"]) / "worker.log")
        lines.append(f"• #{inc['id']} {label(inc)} failed: {f['error'][:200]}\n  Diagnosing now — I'll report back (≤30 min).")
    save_state(st)
    if lines:
        notify("🩺 Upload doctor\n" + "\n".join(lines))
    return 0


# ── diagnosis ────────────────────────────────────────────────
PROMPT = """You are the "upload doctor" for Erik's photo-upload pipeline on his VPS. An unattended
upload just failed. Find out why and, if it is a code problem, prepare a fix. You run with
no human watching; Erik will read your conclusion on Telegram and decide whether to apply it.

## The failure
- Incident: #{id}{test_note}
- Row: {row} "{title}"
- Platform: {pname} (code {platform})
- Source: {source_text}
- Error: {error}
- Run log (summary): {run_log}
- Raw step-by-step log of that run: {raw_log}
- Screenshots from around that time: {shots}
- Row data (from upload_queue.csv): {row_json}

## Where things are
- Live code (READ ONLY for you): /root/photo-upload-workflow  (upload.py has one upload_to_<platform>
  function per platform; logs/ has run_*.log, raw_run_*.log, error screenshots).
- YOUR working copy (a git worktree, edit ONLY here): {repo}
- Your incident folder (scratch scripts, screenshots, notes): {dir}
- Project memory with history of past site redesigns and fixes:
  /root/.claude/projects/-root/memory/project_photo_upload.md  — read the parts about {pname} first.
- To look at the live site, write a small Python script in your incident folder that does
  `import sys; sys.path.insert(0, "/root/autofix"); from autofix_browser import open_page`
  then `with open_page() as page: ...` (logged-in automation browser; it waits for and holds the
  upload lock for you). Run it with /root/photo-upload-workflow/venv/bin/python <script>.
  Dump buttons/inputs/text, take screenshots into your incident folder, read them with the Read tool.

## Hard rules — never break these
1. NEVER publish, post, submit, upload, save, or click any Publish/Post/Upload/Submit/Save/Share
   button on any site. NEVER choose/set a file in any file input or drop zone — on several sites
   (e.g. 35photo) choosing a file already uploads it and creates a public photo page. NEVER type into
   composers/forms. Allowed: opening pages, reading text/DOM, listing buttons/inputs/selectors,
   screenshots, scrolling, opening menus/dialogs that don't send anything. If the failure is in a
   step after the file is chosen, work from the code, the raw log and the run's own error
   screenshots instead, and lower your confidence accordingly.
2. NEVER run upload.py, run_upload.sh, run_nosubmit.sh, or call upload_to_* / functions that post.
3. Do not edit anything outside {repo} and {dir}. No git push/commit, no CSV writes, no
   systemctl, no hermes commands, no deleting files.
4. Keep the fix minimal and in the style of the surrounding code. Don't refactor.
5. Don't hold the browser longer than needed (one open_page block at a time, < 10 min).

## Deliverables (in {dir})
1. Your code changes, if any, edited directly in {repo} (usually just upload.py).
   Check them: `/root/photo-upload-workflow/venv/bin/python -m pyflakes <file>` in {repo} — no new warnings.
2. result.json, exactly these keys:
   {{"cause": "plain-English cause, 1-2 sentences",
    "fix_ready": true/false,
    "confidence": "high" | "medium" | "low",
    "summary": "one Telegram line: what you changed, e.g. 'Publish button renamed to \\"Publish\\"; updated selector'",
    "post_exists": true/false/null,   // is the post actually live on the site right now? null = couldn't tell
    "retry_safe": true/false,         // would retrying this platform for this row be safe (no duplicate)?
    "notes": "anything Erik or Claude should know"}}
3. report.md — what you checked, what you saw, why you think so. Brief.
If it is not a code problem (site down, account issue, data issue), say so with fix_ready=false.
"""


def browser_helper():
    """Write /root/autofix/autofix_browser.py (used by the diagnosis scripts)."""
    (ROOT / "autofix_browser.py").write_text('''"""Logged-in automation browser for upload-doctor diagnosis scripts.
    with open_page() as page: ...
Waits for and holds /root/photo-upload-workflow/.upload.lock while open."""
import os, sys, time
from contextlib import contextmanager
sys.path.insert(0, "/root/photo-upload-workflow")
os.environ.setdefault("DISPLAY", ":99")
import upload as u
from playwright.sync_api import sync_playwright
import playwright.sync_api as _pw

# Hard guard: choosing a file uploads it on some sites (35photo creates a public
# photo page immediately — happened in the first test, 2026-11-12). Diagnosis
# never needs it, so every way of setting a file raises.
def _no_files(*a, **k):
    raise RuntimeError("upload doctor: choosing/uploading files is not allowed in diagnosis")
for _cls in ("Page", "Frame", "Locator", "ElementHandle", "FileChooser"):
    if hasattr(getattr(_pw, _cls, None), "set_input_files"):
        setattr(getattr(_pw, _cls), "set_input_files", _no_files)
LOCK = "/root/photo-upload-workflow/.upload.lock"

def _holder():
    try:
        pid = int(open(LOCK).read().strip()); os.kill(pid, 0); return pid
    except Exception:
        return None

@contextmanager
def open_page(max_wait=25 * 60):
    deadline = time.time() + max_wait
    while _holder() is not None and _holder() != os.getpid():
        if time.time() > deadline:
            raise RuntimeError("browser profile busy (upload running) for too long")
        time.sleep(15)
    open(LOCK, "w").write(str(os.getpid()))
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(u.BROWSER_PROFILE), headless=False, executable_path=u.find_chrome(),
                args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                      "--no-default-browser-check"],
                viewport={"width": 1280, "height": 900}, timezone_id="Asia/Jerusalem", locale="en-IL")
            try:
                page = ctx.new_page()
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                u.apply_stealth(page)
                yield page
            finally:
                u.close_context_gracefully(ctx)
    finally:
        try:
            if open(LOCK).read().strip() == str(os.getpid()):
                os.unlink(LOCK)
        except Exception:
            pass
''')


def make_worktree(inc):
    """Worktree on a throwaway branch, with any uncommitted live changes as its baseline commit."""
    d = inc_dir(inc["id"])
    repo = d / "repo"
    branch = f"autofix/{inc['id']}"
    git("worktree", "prune", check=False)
    git("branch", "-D", branch, check=False)
    git("worktree", "add", "-f", str(repo), "-b", branch, "HEAD")
    dirty = [l[3:] for l in git("status", "--porcelain").splitlines() if l[:2].strip() == "M"]
    for rel in dirty:
        shutil.copy2(SCRIPT_DIR / rel, repo / rel)
    if dirty:
        git("-c", "user.name=autofix", "-c", "user.email=autofix@local", "commit", "-qam",
            "baseline: uncommitted live changes", cwd=repo)
    inc["baseline"] = git("rev-parse", "HEAD", cwd=repo).strip()
    inc["live_md5"] = {p: md5(SCRIPT_DIR / p) for p in git("ls-files", cwd=repo).splitlines()
                       if p.endswith((".py", ".html", ".sh"))}
    return repo


def cmd_diagnose(args):
    inc = load_inc(args.id)
    d = inc_dir(inc["id"])
    try:
        browser_helper()
        repo = make_worktree(inc)
        save_inc(inc)
        try:
            row = next((x for x in u.load_queue(u.DEFAULT_CSV) if x["upload_id"] == inc["row"]), {})
        except Exception:
            row = {}
        keep = ("upload_id", "title", "caption", "keywords", "da_nsfw_flag", "category_500px",
                "category_35p", "platforms", "status", URL_FIELD.get(inc["platform"], ""), "model_name",
                "location_500px", "error_log", "stash_url_nsfw", "stash_url_safe")
        row_json = json.dumps({k: row.get(k, "") for k in keep if k}, ensure_ascii=False)[:3000]
        prompt = PROMPT.format(
            id=inc["id"], row=inc["row"], title=inc.get("title", ""), pname=NAME.get(inc["platform"], inc["platform"]),
            platform=inc["platform"], error=inc["error"], run_log=inc.get("run_log") or "(none)",
            raw_log=inc.get("raw_log") or "(none)", shots=", ".join(inc.get("screenshots") or []) or "(none)",
            row_json=row_json, repo=repo, dir=d,
            test_note=" — THIS IS A TEST: the error below is invented; diagnose the platform's current state "
                      "anyway, as if it were real." if inc.get("test") else "",
            source_text="upload.py reported this platform FAILED" if inc["source"] == "run"
            else "upload.py reported success, but the post-upload check could not find the post on the profile")
        (d / "prompt.md").write_text(prompt)
        live, dd = str(SCRIPT_DIR), str(d)
        allowed = [
            "Read", "Glob", "Grep", "Bash(cd *)",
            f"Edit(/{dd}/**)", f"Write(/{dd}/**)",
            f"Bash({live}/venv/bin/python *)", "Bash(python3 *)",
            "Bash(ls *)", "Bash(ls)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(grep *)",
            "Bash(sed -n *)", "Bash(wc *)", "Bash(diff *)", "Bash(git diff*)", "Bash(git status*)",
            "Bash(git log*)", "Bash(git show*)", "Bash(md5sum *)", "Bash(file *)",
        ]
        denied = [
            f"Edit(/{live}/**)", f"Write(/{live}/**)", "Edit(//root/.hermes/**)", "Write(//root/.hermes/**)",
            "Bash(*python upload.py*)", "Bash(*python ./upload.py*)", "Bash(*python /root/photo-upload-workflow/upload.py*)",
            "Bash(*upload.py --row*)", "Bash(*upload.py --platform*)", "Bash(*run_upload*)", "Bash(*run_nosubmit*)",
            "Bash(*git push*)", "Bash(*git commit*)", "Bash(*git checkout*)", "Bash(*git reset*)",
            "Bash(*systemctl*)", "Bash(hermes *)", "Bash(* hermes *)", "Bash(rm *)", "Bash(* rm *)",
            "Bash(kill *)", "Bash(pkill *)", "Bash(* kill *)", "Bash(*curl *)",
            "WebFetch", "WebSearch",
        ]
        inc["session"] = True
        inc["session_started"] = now().isoformat(timespec="seconds")
        save_inc(inc)
        with open(d / "session.json", "w") as out, open(d / "session.err", "w") as err:
            try:
                r = subprocess.run(
                    [CLAUDE, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
                     "--add-dir", live, "--add-dir", dd, "--add-dir", "/root/autofix",
                     "--add-dir", "/root/.claude/projects/-root/memory",
                     "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied)],
                    input=prompt, cwd=dd, stdout=out, stderr=err, text=True, timeout=SESSION_TIMEOUT,
                    env={**os.environ, "DISPLAY": ":99"})
                inc["session_rc"] = r.returncode
            except subprocess.TimeoutExpired:
                inc["session_rc"] = "timeout"
        finish_diagnosis(inc, repo)
    except Exception as e:
        inc["status"] = "no_fix"
        inc["error_internal"] = str(e)
        save_inc(inc)
        notify(f"🩺 #{inc['id']} {label(inc)}: the diagnosis itself failed ({str(e)[:200]}). "
               f"Ask Claude on the PC to look at /root/autofix/incidents/{inc['id']}.")
    return 0


def pyflakes_count(path):
    r = subprocess.run([PY, "-m", "pyflakes", str(path)], capture_output=True, text=True)
    return len([l for l in r.stdout.splitlines() if l.strip()])


def check_files(base_dir, files):
    """Syntax + pyflakes on changed .py files. Returns a list of problems."""
    probs = []
    for rel in files:
        p = Path(base_dir) / rel
        if rel.endswith(".py"):
            r = subprocess.run([PY, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())", str(p)],
                               capture_output=True, text=True)
            if r.returncode:
                probs.append(f"{rel}: syntax error {r.stderr.strip()[-200:]}")
        elif rel.endswith(".sh"):
            if subprocess.run(["bash", "-n", str(p)], capture_output=True).returncode:
                probs.append(f"{rel}: bash syntax error")
    return probs


def finish_diagnosis(inc, repo):
    d = inc_dir(inc["id"])
    try:
        res = json.loads((d / "result.json").read_text())
    except Exception:
        res = {"cause": "the diagnosis session didn't write a result", "fix_ready": False,
               "confidence": "low", "summary": "", "notes": f"session rc={inc.get('session_rc')}"}
    inc["result"] = res
    try:
        sess = json.loads((d / "session.json").read_text())
        inc["cost_usd"] = sess.get("total_cost_usd")
    except Exception:
        pass
    diff = git("diff", inc["baseline"], cwd=repo)
    files = [f for f in git("diff", "--name-only", inc["baseline"], cwd=repo).splitlines() if f]
    allowed_files = [f for f in files if f.endswith((".py", ".html", ".sh")) and f != "queue_server.py"]
    tag = "🧪 TEST — " if inc.get("test") else ""
    head = f"🩺 {tag}#{inc['id']} {label(inc)}\nCause: {res.get('cause', '?')}"
    if res.get("post_exists") is True:
        head += "\nThe post IS live on the site."
    if res.get("fix_ready") and diff.strip():
        problems = []
        if allowed_files != files:
            problems.append(f"patch touches files it shouldn't: {', '.join(set(files) - set(allowed_files))}")
        problems += check_files(repo, files)
        for rel in files:
            if rel.endswith(".py"):
                base_tmp = d / f"base_{Path(rel).name}"
                base_tmp.write_text(git("show", f"{inc['baseline']}:{rel}", cwd=repo))
                if pyflakes_count(repo / rel) > pyflakes_count(base_tmp):
                    problems.append(f"{rel}: new pyflakes warnings")
        (d / "fix.patch").write_text(diff)
        inc["files"] = files
        stat = git("diff", "--shortstat", inc["baseline"], cwd=repo).strip()
        if problems:
            inc["status"] = "no_fix"
            save_inc(inc)
            notify(head + f"\nA fix was drafted but failed the safety checks ({'; '.join(problems)[:300]}). "
                          f"Not offered. Details: /root/autofix/incidents/{inc['id']}")
            return
        inc["status"] = "ready"
        save_inc(inc)
        if inc["platform"] == "YOUPIC":
            retry = "and let the YouPic catch-up retry it"
        elif res.get("post_exists") is True or not res.get("retry_safe"):
            retry = f"(no retry of {NAME.get(inc['platform'])} — {'post already live' if res.get('post_exists') else 'retry not judged safe'})"
        else:
            retry = f"and retry {NAME.get(inc['platform'])} for {inc['row']} only"
        notify(head + f"\nFix ({res.get('confidence', '?')} confidence, {', '.join(files)}, {stat}): "
                      f"{res.get('summary', '')}\n"
                      f"Reply \"fix {inc['id']}\" to apply it {retry}, or \"skip {inc['id']}\"."
               + (f"\nNote: {res['notes'][:300]}" if res.get("notes") else ""))
    else:
        inc["status"] = "no_fix"
        save_inc(inc)
        cleanup_worktree(inc)
        notify(head + "\nNo code fix proposed." + (f"\n{res.get('notes', '')[:400]}" if res.get("notes") else "")
               + f"\nDetails: /root/autofix/incidents/{inc['id']}/report.md")


# ── apply ────────────────────────────────────────────────────
def cmd_apply(args):
    inc = load_inc(args.id)
    dry = args.dry or inc.get("test")
    if inc["status"] not in ("ready", "retry_only"):
        msg = f"#{inc['id']} can't be applied — status is '{inc['status']}'."
        notify(msg) if args.background else print(msg)
        return 1
    if args.background:
        detach(["apply", str(inc["id"])] + (["--dry"] if dry else []), inc_dir(inc["id"]) / "apply.log")
        print(f"Started applying #{inc['id']} in the background — the result will arrive on Telegram.")
        return 0
    tag = "🧪 TEST (dry run) — " if dry else ""
    d = inc_dir(inc["id"])
    steps = []
    if inc["status"] == "ready":
        files = inc.get("files", [])
        changed = [f for f in files if md5(SCRIPT_DIR / f) != inc["live_md5"].get(f)]
        if changed:
            inc["status"] = "no_fix"
            save_inc(inc)
            notify(f"🩺 #{inc['id']}: not applied — {', '.join(changed)} changed since the diagnosis. "
                   f"Ask Claude to redo it.")
            return 1
        r = subprocess.run(["git", "apply", "--check", str(d / "fix.patch")], cwd=SCRIPT_DIR,
                           capture_output=True, text=True)
        if r.returncode:
            notify(f"🩺 #{inc['id']}: not applied — the patch doesn't apply cleanly ({r.stderr.strip()[:200]}).")
            return 1
        steps.append("patch applies cleanly")
        if dry:
            notify(f"🩺 {tag}#{inc['id']}: patch checks passed ({', '.join(files)}). Nothing changed, nothing retried.")
            return 0
        if not acquire_lock(30 * 60):
            notify(f"🩺 #{inc['id']}: not applied — an upload kept the browser busy for 30 min. Try again later.")
            return 1
        try:
            BACKUPS.mkdir(exist_ok=True)
            stamp = ts()
            for f in files:
                if (SCRIPT_DIR / f).exists():
                    shutil.copy2(SCRIPT_DIR / f, BACKUPS / f"{Path(f).name}.{stamp}.autofix-{inc['id']}")
            steps.append(f"backed up to ~/.hermes-manual-backups (*.{stamp}.autofix-{inc['id']})")
            subprocess.run(["git", "apply", str(d / "fix.patch")], cwd=SCRIPT_DIR, check=True)
            probs = check_files(SCRIPT_DIR, files)
            if probs:
                for f in files:
                    b = BACKUPS / f"{Path(f).name}.{stamp}.autofix-{inc['id']}"
                    if b.exists():
                        shutil.copy2(b, SCRIPT_DIR / f)
                notify(f"🩺 #{inc['id']}: applied patch failed checks ({'; '.join(probs)[:200]}) — rolled back.")
                return 1
            res = inc.get("result", {})
            msgf = d / "commit_msg.txt"
            msgf.write_text(f"fix({NAME.get(inc['platform'], inc['platform']).lower()}): "
                            f"{res.get('summary', 'upload doctor fix')[:60]}\n\n"
                            f"Upload doctor incident #{inc['id']} ({inc['row']}). Approved by Erik on Telegram.\n"
                            f"Cause: {res.get('cause', '')}\n\n"
                            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
            git("add", *files)
            git("commit", "-q", "-F", str(msgf), "--", *files)
            sha = git("rev-parse", "--short", "HEAD").strip()
            steps.append(f"committed {sha}")
            pr = subprocess.run(["git", "push", "-q"], cwd=SCRIPT_DIR, capture_output=True, text=True)
            steps.append("pushed" if pr.returncode == 0 else f"PUSH FAILED ({pr.stderr.strip()[:100]})")
        finally:
            release_lock()
        cleanup_worktree(inc)
    elif dry:
        notify(f"🩺 {tag}#{inc['id']}: would retry {NAME.get(inc['platform'])} for {inc['row']}. Nothing done.")
        return 0
    inc["status"] = "applied"
    inc["applied"] = now().isoformat(timespec="seconds")
    save_inc(inc)
    retry_note = do_retry(inc, steps)
    notify(f"🩺 #{inc['id']} {label(inc)}\n" + "\n".join(f"✓ {s}" for s in steps) + f"\n{retry_note}")
    return 0


def do_retry(inc, steps):
    res = inc.get("result", {})
    p, row = inc["platform"], inc["row"]
    if inc["status"] == "applied" and inc.get("files") is not None:
        if res.get("post_exists") is True:
            return f"Not retried — the post is already live on {NAME.get(p)}."
        if not res.get("retry_safe", False):
            return "Not retried (retry not judged safe). Retry by hand when ready: run_upload / ask Claude."
    if p == "YOUPIC":
        subprocess.run([PY, "youpic_catchup.py", "--schedule-in", "120", "--row", row], cwd=SCRIPT_DIR)
        return "YouPic catch-up scheduled to retry in ~2 min (its own notice will follow)."
    # A verify-sourced failure has a (false) URL recorded — clear it so upload.py doesn't skip.
    field = URL_FIELD.get(p)
    rows = u.load_queue(u.DEFAULT_CSV)
    cur = next((x for x in rows if x["upload_id"] == row), {})
    if field and cur.get(field, "").strip():
        if inc["source"] != "verify":
            return f"Not retried — {field} is already set for {row}."
        shutil.copy2(u.DEFAULT_CSV, BACKUPS / f"upload_queue.csv.{ts()}.autofix-{inc['id']}")
        u.save_row_update(u.DEFAULT_CSV, row, {field: ""})
        steps.append(f"cleared the false {field} (CSV backed up)")
    if not acquire_lock(30 * 60):
        return "Not retried — the browser was busy for 30 min. Reply again later."
    d = inc_dir(inc["id"])
    try:
        with open(d / "retry.log", "w") as fh:
            r = subprocess.run([PY, "upload.py", "--row", row, "--platform", p, "--skip-login-check"],
                               cwd=SCRIPT_DIR, stdout=fh, stderr=subprocess.STDOUT, timeout=UPLOAD_TIMEOUT,
                               env={**os.environ, "DISPLAY": ":99"})
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    finally:
        release_lock()
    fresh = next((x for x in u.load_queue(u.DEFAULT_CSV) if x["upload_id"] == row), {})
    url = fresh.get(field, "").strip() if field else ""
    if url and url not in ("NO_SUBMIT",):
        subprocess.Popen([PY, "verify_upload.py", "--row", row, "--platforms", p, "--delay", "60"],
                         cwd=SCRIPT_DIR, stdout=open(d / "verify.log", "w"), stderr=subprocess.STDOUT,
                         start_new_session=True, env={**os.environ, "DISPLAY": ":99"})
        return f"✅ Retry worked — {url}\n(the usual verification follows in a few minutes)"
    return f"❌ Retry still failed (rc={rc}). Log: /root/autofix/incidents/{inc['id']}/retry.log — ask Claude."


def cmd_skip(args):
    inc = load_inc(args.id)
    if inc["status"] in ("applied",):
        print(f"#{inc['id']} was already applied.")
        return 1
    inc["status"] = "skipped"
    save_inc(inc)
    cleanup_worktree(inc)
    print(f"#{inc['id']} skipped — nothing changed.")
    return 0


def cmd_status(args):
    expire_old()
    incs = all_incs()[-args.n:]
    if not incs:
        print("No incidents yet.")
    for i in incs:
        r = i.get("result", {})
        print(f"#{i['id']:<3} {i['created'][:16]}  {i['row']} {NAME.get(i['platform'], i['platform']):<10} "
              f"{i['status']:<10}{' TEST' if i.get('test') else ''}  {(r.get('summary') or r.get('cause') or i['error'])[:90]}")
    print("Switched OFF" if OFF.exists() else "Switched on")
    return 0


def cmd_test(args):
    st = load_state()
    f = {"row": args.row, "platform": args.platform.upper(), "source": "run",
         "error": args.error, "run_log": "", "raw_log": ""}
    inc = new_incident(st, f, now() - timedelta(hours=1), "diagnosing", test=True)
    save_state(st)
    detach(["diagnose", str(inc["id"])], inc_dir(inc["id"]) / "worker.log")
    print(f"Test incident #{inc['id']} started (diagnosis runs in the background).")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trigger")
    t.add_argument("--since", required=True)
    t.add_argument("--missing-json", default="")
    dg = sub.add_parser("diagnose")
    dg.add_argument("id", type=int)
    a = sub.add_parser("apply")
    a.add_argument("id", type=int)
    a.add_argument("--dry", action="store_true")
    a.add_argument("--background", action="store_true")
    s = sub.add_parser("skip")
    s.add_argument("id", type=int)
    stt = sub.add_parser("status")
    stt.add_argument("-n", type=int, default=10)
    te = sub.add_parser("test")
    te.add_argument("--row", required=True)
    te.add_argument("--platform", required=True)
    te.add_argument("--error", required=True)
    args = ap.parse_args()
    INC.mkdir(parents=True, exist_ok=True)
    return {"trigger": cmd_trigger, "diagnose": cmd_diagnose, "apply": cmd_apply, "skip": cmd_skip,
            "status": cmd_status, "test": cmd_test}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
