---
name: da-upload
description: "Full multi-platform photo upload workflow. Views the Sta.sh photo in Chrome for metadata generation, generates AUTO metadata via Claude vision, shows an approval gate, then uploads sequentially to 500px / 35photo / Facebook via Chrome automation, and finally publishes to DeviantArt via the DA web interface (last, since publishing removes the Sta.sh item). Reads the queue CSV from Google Drive — works identically on Windows and macOS. Run daily or on demand for immediate publishing."
---

# Multi-Platform Photo Upload Workflow

You are executing Erik's daily photo upload automation. All photos are stored on Sta.sh. The critical rule: **DeviantArt must always be published LAST**, because publishing removes the Sta.sh staging item. Follow every step in order without skipping.

> **IMPORTANT — Network constraint:** The Cowork VM cannot reach the DeviantArt API due to proxy sandboxing. Do NOT attempt to run `stash_download.py` or `da_upload.py` — both will fail with a proxy error. All DeviantArt interaction must go through Chrome browser automation.

> **SPEED PRINCIPLES — Target: 5 minutes for DA-only, 10 minutes for all 4 platforms. Follow these rules:**
> 1. **Minimize screenshots** — Only screenshot at three checkpoints per platform: (a) photo view for AI vision, (b) filled form before submit, (c) result after submission. Do NOT screenshot after every individual field or click.
> 2. **Combine JS calls** — When filling forms via `javascript_tool`, batch multiple field operations into a single script instead of making separate calls for each field.
> 3. **Skip homepages** — Navigate directly to upload/submit URLs. Check the account from the upload page itself, not by visiting the homepage first.
> 4. **Pre-built descriptions** — All platform description variants are assembled once in Step 4. During uploads, paste the pre-built text — do not re-derive or re-compose descriptions per platform.
> 5. **No unnecessary waits** — Do not add artificial delays between actions. If a page has loaded, proceed immediately.
> 6. **Screenshot failures** — The Chrome extension screenshot tool frequently fails with "detached" or timeout errors even when the page is live. **Do NOT retry screenshots in a loop.** If a screenshot fails:
>    - Try **once more** after a 2-second wait. If it fails again, **skip it and move on**.
>    - Use `read_page` or `javascript_tool` (e.g. `document.title + ' | ' + document.URL`) to confirm page state instead.
>    - The only screenshot that truly matters is Step 3 (photo view for AI vision). All other screenshots are verification-only — if they fail, proceed based on JS/DOM confirmation instead.
>    - **Never retry more than twice.** Three failed screenshots = move on immediately.

---

## STEP 0 — Confirm Browser Machine & Load Config

**Load config:**

```python
import json, glob, os

patterns = [
    "/sessions/*/mnt/**/config.json",
    "/sessions/*/mnt/config.json",
]
config_path = next((m for p in patterns for m in glob.glob(p, recursive=True)), None)
if not config_path:
    print("CONFIG_NOT_FOUND")
else:
    with open(config_path) as f:
        config = json.load(f)
    print(json.dumps(config, indent=2))
```

If `CONFIG_NOT_FOUND`: tell the user and stop — the config.json file is missing from the queue folder.

If any account value still contains `YOUR_`: tell the user which platforms have placeholder values and ask them to fill in `config.json` before continuing.

**Detect browser machine automatically:**

Call `tabs_context_mcp` to get a tab ID, then run:

```javascript
// javascript_tool on any available tab:
navigator.userAgent + " | platform: " + navigator.platform
```

Parse the result:
- If it contains `Win` → report: *"🖥 Browser is running on **Windows**"*
- If it contains `Mac` → report: *"🍎 Browser is running on **macOS**"*
- Otherwise → report the raw string

Tell the user which machine the browser is on. If it is not the expected machine, ask: *"The browser is on [detected machine]. Is that correct?"* and wait for confirmation before proceeding. If it is the expected machine, continue automatically.

---

## STEP 1 — Find the Queue CSV

Search for `upload_queue.csv` in the Cowork mounted folder:

```python
import csv, datetime, glob, json, os, sys

patterns = [
    "/sessions/*/mnt/**/upload_queue.csv",
    "/sessions/*/mnt/upload_queue.csv",
]
queue_path = next((m for p in patterns for m in glob.glob(p, recursive=True)), None)

if not queue_path:
    print("QUEUE_NOT_FOUND"); sys.exit(1)

queue_dir = os.path.dirname(queue_path)
today = datetime.date.today().isoformat()

with open(queue_path, newline='', encoding='utf-8') as f:
    rows = list(csv.DictReader(f))

pending_today = [r for r in rows
    if r.get('scheduled_date','').strip() == today
    and r.get('status','').strip() == 'Pending']

upcoming = sorted(
    [r for r in rows if r.get('status','').strip() == 'Pending'
     and r.get('scheduled_date','') > today],
    key=lambda r: r['scheduled_date']
)[:5]

print(f"QUEUE={queue_path}")
print(f"QUEUE_DIR={queue_dir}")
print(f"TODAY={today}")
print(f"PENDING_TODAY={len(pending_today)}")
if pending_today:
    r = pending_today[0]
    print(json.dumps(dict(r), indent=2))
else:
    print("UPCOMING=" + json.dumps([
        {'id': r['upload_id'], 'date': r['scheduled_date'], 'title': r['title']}
        for r in upcoming
    ], indent=2))
```

**If QUEUE_NOT_FOUND:** Tell the user and stop. They need to confirm Cowork has the Google Drive queue folder selected.

**If no PENDING_TODAY:** Show the next 5 upcoming entries. Ask: *"Nothing scheduled for today. Which entry would you like to publish now, or should I wait for tomorrow?"* Only proceed after explicit user selection.

**If the target row already has `status = Uploaded`:** Stop and warn the user: *"This entry is already marked Uploaded (DA: {da_deviation_url}). Re-uploading would create a duplicate. Do you want to proceed anyway?"* Do not continue unless they explicitly confirm.

---

## STEP 2 — Show Today's Photo

Display a clean summary of the target row:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📸  {upload_id}  ·  {scheduled_date} at {scheduled_time}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sta.sh NSFW:  {stash_url_nsfw}
Sta.sh SAFE:  {stash_url_safe}
Title:        {title}
Caption:      {caption}
Keywords:     {keywords}
Platforms:    {platforms}
  DA NSFW flag:   {da_nsfw_flag}
  500px category: {category_500px}
  35p category:   {category_35p}
  FB caption:     {caption_fb or "(uses main caption, links stripped)"}
  DA Gallery:     {da_gallery or "Featured"}
  DA Groups:      {da_groups or "(none)"}
  Model:          {model_name or "(none)"}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## STEP 3 — View Photo in Chrome

**Do this BEFORE any uploads or metadata generation.** View the image in Chrome so you can generate metadata. DA must remain unpublished until all other platforms are done.

Get a browser tab via `tabs_context_mcp`. Navigate directly to the Sta.sh URL:

```
https://sta.sh/{stash_id_from_stash_url_nsfw}
```

Take a screenshot — this is the **primary image screenshot** used for AI vision in Step 4. Confirm the photo loads and you can see it clearly. Zoom in if needed for better detail.

**If the photo shows "Page not found" or is missing:** Stop. Tell the user the Sta.sh URL may be invalid or the item was already published.

**For platforms that need a local file (500px, 35photo, Facebook):** The file must be on the user's local machine — the VM cannot download it via the API. The expected path is:
- macOS: `~/Library/CloudStorage/GoogleDrive-.../My Drive/photography/queue/temp/{upload_id}_NSFW.jpg`
- Windows: `G:\My Drive\photography\queue\temp\{upload_id}_NSFW.jpg`

If the file is not there, ask the user to save it from Sta.sh to that location before proceeding with those platforms. DA does not need a local file — it publishes directly from Sta.sh via the browser.

---

## STEP 4 — Generate AUTO Metadata

For any field containing exactly `AUTO`, use the screenshot from Step 3 to analyze the image with your vision capabilities. Generate:

- **Title** (3–8 words, evocative, classical): e.g. *"Afternoon Light on Film"*, *"Study in Natural Shadow"*
- **Caption** (2–3 sentences, 150–250 chars): classical fine art voice, mention Yashica MAT-124G / medium format / analog when visible
- **Keywords** — Generate **exactly 30 tags** for DeviantArt (hard limit — DA silently blocks form submission above 30 tags with no error message, even though the UI appears to accept more). Use no-space or CamelCase for multi-word concepts — DA splits on spaces, so `long exposure` becomes two useless tags `long` and `exposure`; write it as `longexposure` instead. If you generate more than 30, trim to the top 30 most relevant.

  Build the tag list across these categories:

  | Category | Examples |
  |---|---|
  | Location | `Bangkok`, `Thailand`, `SoutheastAsia`, `WatSaket` |
  | Subject | `temple`, `architecture`, `GoldenMount`, `pagoda` |
  | Technique | `longexposure`, `blackandwhite`, `monochrome`, `slowshutter` |
  | Mood/aesthetic | `dramatic`, `moody`, `atmospheric`, `dark`, `stormy` |
  | Composition | `lowangle`, `wideangle`, `silhouette`, `framing` |
  | Genre | `travelphotography`, `landscapephotography`, `urbanphotography`, `streetphotography` |
  | Discovery terms | `photography`, `blackandwhitephotography`, `fineart`, `photoart` |
  | Gear | `Fujifilm`, `XT20`, `FujifilmXT20`, `mirrorless` |

  Adapt categories to the actual image — not all categories apply to every photo. Aim for the fullest accurate list, not a padded one.

  For **500px and 35photo**, use a shorter curated subset of 10–15 of the most relevant tags from the same list (those platforms have tighter limits and benefit from precision over volume).

### Model Respect Injection

If `model_name` is not empty in the CSV row, append the following to the **end** of the final description text for **all platforms** (DA, 500px, 35photo, and Facebook):

```
Model: {model_name}. Please respect the model.
```

This line is appended after any AUTO-generated or manually-provided caption text. It should appear in the proposed metadata shown to the user before the approval gate.

### Social Links Injection

Read `social_links` from `config.json`. If the `social_links` object exists and has entries, append them as a formatted block at the very end of the description for **DA, 500px, and 35photo only** (NOT Facebook — Facebook penalizes posts with external URLs).

Format appended to the description:

```
---
Web: erikrozman.com/
IG: www.instagram.com/rozmane/
500px: 500px.com/erikrozman
DA: flyy1.deviantart.com/
```

Only include links that are present and non-empty in the config. The social links block goes after the model respect line (if present) or after the caption (if no model).

**Do NOT add social links to the Facebook caption.**

### Pre-assemble Platform Descriptions

Build all description variants **once** and store them in memory. Do not re-derive these during uploads — just paste the pre-built text.

| Variable | Content | Used by |
|---|---|---|
| `desc_full` | `{final_caption}` + model respect line + social links block | DA, 500px, 35photo |
| `desc_fb` | `{caption_fb or final_caption stripped of URLs}` + model respect line (NO social links) | Facebook |
| `tags_da` | Full 30-tag list (CamelCase, no spaces) | DA |
| `tags_short` | Top 10–15 curated subset | 500px, 35photo |

Show the proposed metadata clearly — including both description variants and both tag sets — before proceeding to the approval gate.

---

## STEP 5 — Approval Gate

**This is the hard stop. No browser tabs are opened, no forms are touched, and no uploads of any kind begin until the user approves here.**

Use AskUserQuestion with:
- **Approve & Publish** — Run all platforms in `{platforms}` with the metadata shown
- **Edit Metadata** — Change title/caption/keywords before uploading
- **Skip Today** — Mark Skipped, do not upload anything

If **Edit Metadata**: ask what to change, update in memory, re-show the full plan, confirm again before proceeding.

If **Skip Today**: update CSV `status = Skipped`, `notes = "Skipped by user {date}"`. Stop. Do not open any browser tabs.

---

## STEP 5B — Batch Account Verification

**After approval, verify ALL target platform accounts before starting any uploads.** This catches login/account issues upfront instead of mid-workflow.

For each platform in `{platforms}`, navigate directly to its upload page (not homepage) in a new tab and verify the logged-in account matches `config.accounts`:

| Platform | Direct URL | Check |
|---|---|---|
| 500PX | `https://500px.com/photo/upload` | Username matches `config.accounts["500px"]` |
| 35P | `https://35photo.pro/en/upload` | Username matches `config.accounts["35photo"]` |
| FB | `https://www.facebook.com` | Profile name matches `config.accounts["facebook"]` |
| DA | `https://www.deviantart.com` | Username matches `config.accounts["deviantart"]` |

Do this as a quick pass — open each tab, use `read_page` or JS to extract the username, compare. **One screenshot max for all checks combined** (only if there's a mismatch). If all accounts match, report: *"All accounts verified — proceeding with uploads."* and continue immediately.

If any account is wrong or logged out, stop and report all mismatches at once so the user can fix them all before uploads begin.

---

## STEP 6 — Upload to Each Platform

Run platforms in this order: **500px → 35photo → Facebook → DeviantArt (last)**.

DA must be last because publishing consumes the Sta.sh item. The other platforms use the locally downloaded files.

Only run platforms listed in the `platforms` field of the CSV row.

**Since accounts were already verified in Step 5B, skip the account check inside each platform section below — go straight to uploading.**

---

### 6A. 500px (if "500PX" in platforms)

**Navigate directly** to `https://500px.com/photo/upload` (account already verified in Step 5B).

**Upload the file:**
Use `find` or `read_page` to locate the file input element. Trigger it:
- Use `form_input` on the file input with the NSFW file path
- OR click the upload button and use the file picker dialog:
  - Navigate to Google Drive → photography → queue → temp → `{upload_id}_NSFW.jpg`

Wait for upload progress to reach 100%.

**Fill metadata fields:**
1. Title → `{final_title}`
2. Description → paste `desc_full` (pre-built in Step 4)
3. Tags → `{tags_short}` (enter comma-separated)
4. Category → `{category_500px}` (select from dropdown)

**Publish:** Click Publish. Wait for confirmation. Take a screenshot of the result.

Extract the resulting photo URL (e.g. `https://500px.com/photo/...`). Store as `url_500px`.

**On failure:** Take a screenshot, log the error, continue to the next platform.

---

### 6B. 35photo (if "35P" in platforms)

**Navigate directly** to `https://35photo.pro/en/upload` or the upload section (account already verified in Step 5B).

**Upload the NSFW file** using the file input or drag-and-drop area:
- Navigate the file picker to Google Drive → photography → queue → temp → `{upload_id}_NSFW.jpg`

**Fill metadata:**
1. Title → `{final_title}`
2. Description/Comment → paste `desc_full` (pre-built in Step 4)
3. Tags → `{tags_short}`
4. Category → `{category_35p}` (select from available genres)

**Submit.** Wait for confirmation. Take a screenshot of the result.

Extract the resulting photo URL. Store as `url_35p`.

**On failure:** Log and continue.

---

### 6C. Facebook (if "FB" in platforms)

**Navigate** to `https://www.facebook.com` (account already verified in Step 5B). Go to the user's profile or create a post from the home feed.

**Create a new post with photo:**
- Click "Photo/Video" or "Add photos/video" in the post composer
- Use the file picker to select the SAFE file:
  - Navigate to Google Drive → photography → queue → temp → `{upload_id}_SAFE.jpg`

Wait for the photo thumbnail to appear in the composer.

**Fill the caption:** Paste `desc_fb` (pre-built in Step 4 — already has model respect line, no social links).

**Post.** Click the Post button. Wait for the post to appear. Take a screenshot of the result.

Try to extract the post URL. Store as `url_fb` (may not always be available immediately).

**On failure:** Log the error. If you see a CAPTCHA or security check, stop and tell the user: *"Facebook is showing a security check — please complete it manually, then let me know when to continue."*

---

### 6D. DeviantArt via Browser (ALWAYS LAST)

Run this after all other platform uploads are complete. **Do NOT use `da_upload.py` — the VM cannot reach the DeviantArt API.**

**Skip account check for DA** — navigating to the Sta.sh URL inherently proves the user is logged in (Sta.sh items are only visible to their owner). If the Sta.sh page loads and shows the photo, the correct account is active. No separate verification needed.

**Navigate directly** to the Sta.sh URL: `{stash_url_nsfw}`

Scroll below the photo until you see the "Edit or Submit" button (next to "Sell Deviation"). Click it.

**Fill the submission form:**

> **IMPORTANT — Browser interaction rules for DA forms:**
> DeviantArt uses React + ProseMirror editors. Coordinate-based clicks are unreliable because the page is rendered at a different resolution than screenshots. Always prefer JavaScript-based interaction via `javascript_tool`:
> - Use `document.querySelector()` to find elements, then `.scrollIntoView()` before interacting
> - Use `.focus()` on ProseMirror editors before dispatching input
> - Use `el.click()` via JS instead of coordinate-based clicks where possible
> - If a JS approach fails, fall back to coordinates only as a last resort

**Items 1–4 (Title, Mature, Tags, Description) — execute in a SINGLE `javascript_tool` call:**

```javascript
// ── DA FORM MEGA-SCRIPT ──────────────────────────────────────
// Run this as ONE javascript_tool call to avoid multiple round-trips.
// Replace {final_title}, {tags_da_csv}, {desc_full}, {da_nsfw_flag} with actual values.

const results = {};

// 1. TITLE — ProseMirror contenteditable
const titleEl = document.querySelector(
  '[data-hook="title-editor"] [contenteditable], .title-input [contenteditable], input[name="title"]'
);
if (titleEl) {
  titleEl.focus();
  document.execCommand('selectAll');
  document.execCommand('insertText', false, '{final_title}');
  results.title = titleEl.textContent.trim();
} else {
  results.title = 'ELEMENT_NOT_FOUND';
}

// 2. MATURE CHECKBOX — only check if da_nsfw_flag == TRUE
if ('{da_nsfw_flag}' === 'TRUE') {
  const mature = document.querySelector('input[type="checkbox"][name*="mature"], input[type="checkbox"][data-hook*="mature"]');
  if (mature && !mature.checked) mature.click();
  results.mature = 'checked';
} else {
  results.mature = 'skipped';
}

// 3. TAGS — batch inject, hard limit 30
// Remove existing tags
document.querySelectorAll('[data-hook="tag"] button, .tag-remove, .tag .remove').forEach(x => x.click());

const tagInput = document.querySelector(
  '[data-hook="tag-input"] input, input[placeholder*="tag"], input[placeholder*="Tag"]'
);
if (tagInput) {
  tagInput.focus();
  document.execCommand('insertText', false, '{tags_da_csv}');
  tagInput.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
}

// 4. DESCRIPTION — ProseMirror contenteditable
const descEl = document.querySelector(
  '[data-hook="description-editor"] [contenteditable], .description [contenteditable]'
);
if (descEl) {
  descEl.focus();
  document.execCommand('selectAll');
  document.execCommand('insertText', false, `{desc_full}`);
  results.desc = 'set';
} else {
  results.desc = 'ELEMENT_NOT_FOUND';
}

// Brief pause then count tags
await new Promise(r => setTimeout(r, 500));
results.tagCount = document.querySelectorAll('[data-hook="tag"], .tag-item, .tag').length;
JSON.stringify(results);
// ── END MEGA-SCRIPT ──────────────────────────────────────────
```

**After the mega-script returns**, check the `results` object:
- If `title === 'ELEMENT_NOT_FOUND'`: fall back to triple-click + type, but always `.focus()` first
- If `tagCount > 30`: remove extras via JS. If `tagCount === 0`: batch injection failed — fall back to one-at-a-time via JS (`.focus()` + `insertText` + Enter keydown per tag, never coordinate-based)
- If `desc === 'ELEMENT_NOT_FOUND'`: fall back to clicking the description field and typing
**Items 5–7 + Validation + Submit — execute in a SECOND `javascript_tool` call:**

Before running this script, take **one screenshot** of the filled form (after the mega-script) to visually verify title, tags, and description look correct.

Then run this script, which handles Gallery, Groups, Free Download, validation, and Submit in one call:

```javascript
// ── DA POST-FORM SCRIPT ──────────────────────────────────────
// Run as ONE javascript_tool call after verifying the mega-script results.
// Replace {da_gallery_csv}, {da_groups_json}, {should_submit} with actual values.
// {da_gallery_csv} = e.g. "Featured,Street" (from CSV)
// {da_groups_json} = e.g. [{"group":"Street-Shooters","folder":"Candid"}] (parsed from CSV)

const r = {};

// 5. GALLERY — click the Gallery dropdown and select galleries
const galleries = '{da_gallery_csv}'.split(',').map(g => g.trim()).filter(Boolean);
if (galleries.length > 0 && !(galleries.length === 1 && galleries[0] === 'Featured')) {
  // Gallery selector needs interaction — click to open, then select
  const galBtn = document.querySelector('[data-hook="gallery-selector"], .gallery-select, button[class*="gallery"]');
  if (galBtn) {
    galBtn.scrollIntoView({block: 'center'});
    galBtn.click();
    r.gallery = 'opened_selector';
    // Note: selecting specific galleries from the dropdown requires
    // waiting for the panel to render, then clicking the target option.
    // This is platform-specific UI — if the JS selector above doesn't open
    // the gallery panel, fall back to coordinate-based click on the dropdown.
  } else {
    r.gallery = 'selector_not_found — use coordinate click';
  }
} else {
  r.gallery = 'Featured_only_no_change';
}

// 6. GROUPS — handled after this script if da_groups is set
// Groups require a multi-step dialog (click Add → search group → select folder)
// which can't reliably be done in a single synchronous script.
r.groups = '{da_groups_json}' !== '[]' ? 'NEEDS_INTERACTION' : 'none';

// 7. DISABLE FREE DOWNLOAD — expand Advanced settings, toggle the slider off
const advHeader = Array.from(document.querySelectorAll('h2, h3, button, summary, [role="button"]'))
  .find(el => el.textContent?.toLowerCase().includes('advanced'));
if (advHeader) {
  advHeader.scrollIntoView({block: 'center'});
  advHeader.click(); // expand Advanced settings
}
await new Promise(r => setTimeout(r, 300)); // wait for expand animation

const freeDownload = Array.from(document.querySelectorAll('input[type="checkbox"], [role="switch"], label'))
  .find(el => {
    const text = el.textContent || el.getAttribute('aria-label') || '';
    return text.toLowerCase().includes('free download');
  });
if (freeDownload) {
  // If it's a label wrapping a checkbox, find the input
  const toggle = freeDownload.querySelector('input') || freeDownload;
  if (toggle.checked || toggle.getAttribute('aria-checked') === 'true') {
    toggle.click(); // switch OFF
  }
  r.freeDownload = 'disabled';
} else {
  r.freeDownload = 'toggle_not_found — use coordinate click';
}

// VALIDATION — check tag count and title before submit
r.tagCount = document.querySelectorAll('[data-hook="tag"], .tag-item, .tag').length;
r.title = document.querySelector('[data-hook="title-editor"] [contenteditable], .title-input [contenteditable], input[name="title"]')?.textContent?.trim() || '';
r.canSubmit = r.tagCount > 0 && r.tagCount <= 30 && r.title.length > 0;

// SUBMIT — only if validation passes
if (r.canSubmit && {should_submit}) {
  const btn = document.querySelector('button[data-hook="submit-button"], button[type="submit"], button.submit-button');
  if (btn) {
    btn.scrollIntoView({behavior: 'smooth', block: 'center'});
    btn.click();
    r.submitted = true;
  } else {
    r.submitted = false;
    r.submitNote = 'button_not_found — use coordinate click';
  }
} else {
  r.submitted = false;
  if (r.tagCount > 30) r.submitNote = 'TOO_MANY_TAGS';
  if (r.tagCount === 0) r.submitNote = 'NO_TAGS';
  if (!r.title) r.submitNote = 'NO_TITLE';
}

JSON.stringify(r);
// ── END POST-FORM SCRIPT ─────────────────────────────────────
```

**After the post-form script returns**, check the `r` object:
- If `gallery === 'selector_not_found'`: fall back to coordinate-based click on the Gallery dropdown
- If `groups === 'NEEDS_INTERACTION'`: handle each `Group:Folder` pair manually — click the "Submit to Group" / "Add" button, search for the group name, select the folder. This requires sequential UI interaction that can't be fully batched.
- If `freeDownload === 'toggle_not_found'`: scroll to Advanced settings manually, find the "Allow free download" slider, click it off
- If `submitted === false` due to validation: fix the issue (remove extra tags, re-enter title), then re-run validation + submit
- If `submitted === false` due to button not found: fall back to coordinate-based Submit click with `scrollIntoView()` first

**After submission:** Wait for redirect to the live deviation page. URL format: `https://www.deviantart.com/flyy1/art/{deviation_id}`. Take a screenshot and capture as `deviation_url`.

If a "Boost for more visibility" dialog appears, click **Maybe Later** to dismiss it.

This is the point at which the Sta.sh item is consumed — it will no longer be accessible at the original Sta.sh URL.

---

## STEP 7 — Update Queue CSV

Update the row with all results:

```python
import csv, datetime, os

queue_path = "{queue_path}"
upload_id = "{upload_id}"

results = {
    'status': 'Uploaded',   # or 'Partial' if some platforms failed
    'upload_timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'da_deviation_url': "{deviation_url or ''}",
    'url_500px': "{url_500px or ''}",
    'url_35p': "{url_35p or ''}",
    'url_fb': "{url_fb or ''}",
    'error_log': "{comma-separated list of any platform failures}",
}

rows = []
with open(queue_path, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows = list(reader)

for row in rows:
    if row['upload_id'] == upload_id:
        row.update(results)
        break

with open(queue_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print("CSV_UPDATED")
```

**Status logic:**
- All platforms succeeded → `status = Uploaded`
- Some platforms failed → `status = Partial`
- All platforms failed → `status = Failed`

---

## STEP 8 — Clean Up Temp Files (if applicable)

If the user had manually saved local files to the temp folder for 500px / 35photo / Facebook uploads, delete them now:

```python
import os, glob
temp_dir = "{queue_dir}/temp"
upload_id = "{upload_id}"
for suffix in ['_NSFW', '_SAFE']:
    for ext in ['.jpg', '.png', '.tif', '.webp']:
        for f in glob.glob(f"{temp_dir}/*{upload_id}{suffix}{ext}"):
            os.remove(f)
            print(f"Deleted: {f}")
```

If no local files were used (DA-only workflow), skip this step.

---

## STEP 9 — Final Summary

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅  Upload Complete — {upload_id}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DeviantArt:  {da_deviation_url or "skipped"}
500px:       {url_500px or "skipped/failed"}
35photo:     {url_35p or "skipped/failed"}
Facebook:    {url_fb or "skipped/failed"}

Next up: {next_upload_id} on {next_date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

If any platforms failed, tell the user which ones and why. Suggest they check the error_log column in the queue manager and reset to Pending for manual retry if needed.

---

## Error Reference

| Situation | Action |
|---|---|
| Sta.sh URL shows "not found" | Stop — URL may be wrong or item already published. Check the CSV. |
| DA API / stash_download.py used by mistake | Stop immediately. The VM proxy blocks all DA API calls. Use browser only. |
| DA browser: not logged in | Pause and ask user to log in. Wait for confirmation before continuing. |
| DA browser: "Edit or Submit" not visible | Scroll below the photo. It appears below the image, next to "Sell Deviation". |
| DA browser: submission fails silently | Most likely cause: more than 30 tags. Check tag count via JS and remove extras, then retry. |
| DA browser: submission fails with error | Take a screenshot and report the error. Do not retry without user confirmation. |
| 500px upload fails | Log error, continue to 35photo |
| 35photo upload fails | Log error, continue to Facebook |
| Facebook CAPTCHA | Pause and ask user to solve manually, then confirm to continue |
| Facebook post fails | Log error, continue to DA |
| File picker can't find temp file | Ask user to save the file from Sta.sh to the queue temp folder first |
| DA "Boost" dialog appears after submit | Click "Maybe Later" to dismiss — do not boost |
| Screenshot fails / detached error | Try once more after 2s. If still fails, skip — use `read_page` or JS `document.title + document.URL` to confirm page state. Never retry more than twice. |

## On-Demand Publishing

If the user says "publish now" or references a specific upload_id rather than today's scheduled photo, skip the date filter and take the requested row. Always confirm: *"This is scheduled for {date}. Publish it now?"*
