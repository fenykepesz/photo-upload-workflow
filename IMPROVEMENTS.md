# Code Improvement Plan

Derived from a full code review (April 2026). 245 issues identified; this plan covers the top 10 by operational risk. Goal: fix carefully without disrupting daily upload operations.

## Recommended Sequencing

```
Week 1 — data safety, no behavior change:
  #1 Atomic writes  ·  #10 requirements.txt ✓

Week 2 — correctness bugs, low risk:
  #5 Token refresh  ·  #6 Platform filter  ·  #3 --platform status

Week 3 — reliability improvements:
  #4 IG status_code polling  ·  #8 Image pre-validation

Week 4 — hardening:
  #2 Concurrent write lock  ·  #7 CSV injection  ·  #9 COLS versioning
```

---

## Items

### #1 — Atomic CSV writes `save_row_update` · Week 1
**Status:** [x] Done — `c848865`

**Issue:** `save_row_update` writes directly to `upload_queue.csv`. A crash or forced quit mid-write produces a half-written, unrecoverable file.

**Impact:** Low probability, catastrophic — the entire queue is lost.

**Approach:** Write to `upload_queue.csv.tmp` then `os.replace()` (atomic on all OS). If interrupted, `.tmp` is discarded and the original is untouched.

**File:** `upload.py` — `save_row_update()` only.

**Effort:** S · **Fix risk:** Very low — only the failure path changes.

---

### #2 — Concurrent write race (Python + JS 5s poller) · Week 4
**Status:** [ ] Pending

**Issue:** The dashboard's 5s poller can read and re-save the CSV at the exact moment `upload.py` is writing, causing one to overwrite the other.

**Impact:** Rare but can silently wipe a freshly-written `url_ig`, `url_500px`, or status update.

**Approach:** Python writes `upload_queue.csv.lock` before writing and deletes it after. JS poller checks for the lock file and skips that tick if present.

**Files:** `upload.py` — `save_row_update()` · `queue_manager.html` — `setInterval` poller.

**Effort:** M · **Fix risk:** Low — worst case: JS misses one 5s tick.

**Depends on:** #1 (atomic writes should be in place first).

---

### #3 — `--platform` flag fabricates "Uploaded" status · Week 2
**Status:** [x] Done — `7198e00`

**Issue:** Running `upload.py --row X --platform IG` narrows `platforms` to `{"IG"}`, so the done-check passes after only IG succeeds and the row is marked `Uploaded` — the other 7 platforms are silently abandoned.

**Impact:** Active — any `--row --platform` run risks locking out remaining platforms permanently.

**Approach:** When `--platform` is specified, never write `status = "Uploaded"`. Status becomes `Uploaded` only when all platforms in the row's `platforms` column are confirmed done (re-read URLs from a fresh CSV read).

**File:** `upload.py` — status derivation block in `main()`.

**Effort:** S · **Fix risk:** Very low — only affects `--platform` runs.

---

### #4 — Instagram: poll `status_code` before publishing · Week 3
**Status:** [x] Done — `838632b`

**Issue:** A fixed 8s sleep between container creation and publish. Meta requires polling `status_code` until `FINISHED`. Large images or slow Meta servers silently produce `MEDIA_NOT_READY` errors.

**Impact:** Active — causes intermittent IG upload failures on large files.

**Approach:** Replace `time.sleep(8)` with a polling loop: check `status_code` every 5s, up to 12 attempts (60s total). Raise a clear error if `ERROR` status is returned.

**File:** `upload.py` — `upload_to_instagram()`.

**Effort:** S · **Fix risk:** Very low — same API calls, just waits smarter.

---

### #5 — Token refresh records success date on failure · Week 2
**Status:** [x] Done — `7198e00`

**Issue:** `token_last_refreshed = today` is written even when the refresh API returns a 400 error. The 30-day window restarts on a failed refresh — broken credentials go undetected.

**Impact:** Active — already triggered once. Masks broken token until expiry.

**Approach:** Only write `token_last_refreshed` after a confirmed HTTP 200 + valid token in the response body. On failure, leave the date unchanged so the next run retries.

**File:** `upload.py` — token refresh block.

**Effort:** S · **Fix risk:** Minimal — only changes the failure path.

---

### #6 — Platform filter substring match for `"X"` · Week 2
**Status:** [x] Done — `7198e00`

**Issue:** `r.platforms.includes('X')` is a string `includes()` call — matches `"X"` inside `"FBX"`, `"XXX"`, or any future platform code containing X.

**Impact:** Low currently (no collisions), but fragile. One mistyped CSV entry filters incorrectly.

**Approach:** Change all 8 platform checks in `applyFilters` to `r.platforms.split(',').map(p => p.trim()).includes('X')`.

**File:** `queue_manager.html` — `applyFilters()`.

**Effort:** S · **Fix risk:** Very low — purely defensive.

---

### #7 — CSV formula injection protection · Week 4
**Status:** [ ] Pending

**Issue:** `esc()` escapes HTML entities but does not protect against CSV formula injection. Fields starting with `=`, `+`, `-`, `@` execute as formulas when the CSV is opened in Excel or Google Sheets. A Claude-generated keyword could embed `=IMPORTXML(...)`.

**Impact:** Low probability, but if the CSV is ever opened in a spreadsheet app, malicious content could execute.

**Approach:** In `esc()`, after HTML escaping, prefix values starting with `=`, `+`, `-`, or `@` with a tab character (`\t`) — the OWASP-recommended CSV injection mitigation.

**File:** `queue_manager.html` — `esc()`.

**Effort:** S · **Fix risk:** Very low — normal keywords and titles never start with those characters.

---

### #8 — Image pre-validation before IG upload · Week 3
**Status:** [x] Done — `838632b`

**Issue:** Instagram rejects images that are too large (>8MB), wrong format, or wrong aspect ratio — but only after Cloudinary upload + container creation + 8s wait. Failure wastes ~30s and a Cloudinary credit.

**Impact:** Low currently (images are likely well-formed), grows as a risk with NSFW safe-version images.

**Approach:** Before `upload_image_to_cloudinary()`, check: file size ≤ 8MB, extension in `{jpg, jpeg, png}`, shortest dimension ≥ 320px using `PIL.Image.open()`. Fail fast with a clear message.

**Files:** `upload.py` — new helper + one call site. `requirements.txt` — add `Pillow`.

**Effort:** M · **Fix risk:** Low — only adds a pre-check before existing code.

**Depends on:** `Pillow` added to `requirements.txt` (placeholder already in place from #10).

---

### #9 — `COLS` synchronization — documented and version-stamped · Week 4
**Status:** [ ] Pending

**Issue:** `COLS` is defined independently in `upload.py` and `queue_manager.html`. Any new column must be manually mirrored. Drift has already caused bugs (`url_ig`, `ig_tag_people` missing from HTML COLS).

**Impact:** Silent data loss when a column exists in Python but not in JS — it is never written to CSV on save.

**Approach:** Add a `COLS_VERSION = "2.6.0"` constant at the top of both files with a comment listing all columns in order. Add a startup warning in `save_row_update` if the CSV on disk contains a column not in `COLS`.

**Files:** `upload.py` · `queue_manager.html`.

**Effort:** S · **Fix risk:** Very low — no behavior change.

---

### #10 — `requirements.txt` and dependency pinning · Week 1
**Status:** [x] Done — `c5fd7f3`

**Issue:** No `requirements.txt` with pinned versions. A reinstall or new machine could get incompatible package versions.

**What was done:** Pinned `playwright==1.58.0`, `playwright-stealth==2.0.3`, `requests==2.32.5`. Added `Pillow` as a commented placeholder for plan item #8.

---

## Reference: Full Review Severity Summary

- **Critical:** #2/#1 (CSV corruption), #3 (fabricated Uploaded), security items, #9 (COLS drift)
- **High:** Wrong NSFW toggle, no real post URL returned, IG publish too early, CSV injection, no tests/pinning/CI
- **Medium:** Architecture, performance, missing features
- **Low:** Naming, styling, redundancy
