# 📸 Daily Photo Upload Workflow

Automated daily photo publishing to DeviantArt (and optionally 500px, 35photo, and Facebook) using [Claude Cowork](https://claude.ai) as the orchestration engine. Photos are staged on Sta.sh, scheduled via a Google Drive-synced queue, and published through browser automation.

---

## How It Works

Each day, Claude reads the upload queue, finds that day's scheduled photo, views it on Sta.sh, generates metadata (or uses what you've pre-filled), waits for your approval, then publishes to each platform in sequence — always ending with DeviantArt, since publishing to DA consumes the Sta.sh staging item.

```
Sta.sh (staging) → Claude views photo → Generates metadata
→ You approve → 500px → 35photo → Facebook → DeviantArt (last)
→ Queue CSV updated with result URLs
```

---

## Folder Structure

```
photo-queue/
├── upload_queue.csv        # The upload schedule (synced via Google Drive)
├── config.json             # Platform account names for login verification
├── queue_manager.html      # Visual queue manager (open in browser)
├── temp/                   # Temporary local files for non-DA platforms
└── .claude/
    └── skills/
        └── da-upload/
            └── SKILL.md    # Workflow skill definition (read by Claude)
```

---

## Setup

### 1. Prerequisites

- [Claude desktop app](https://claude.ai/download) with **Cowork mode** enabled
- **Claude in Chrome** extension installed in your browser
- **Google Drive** synced locally (on Mac or Windows)
- Cowork pointed at the `photo-queue` folder in Google Drive

### 2. config.json

Edit `config.json` to set your username on each platform. Claude uses these to verify the correct account is logged in before publishing.

```json
{
  "accounts": {
    "deviantart": "flyy1",
    "500px": "YOUR_500PX_USERNAME",
    "35photo": "YOUR_35PHOTO_USERNAME",
    "facebook": "YOUR_FACEBOOK_NAME"
  }
}
```

Replace the placeholder values with your real usernames before using those platforms.

### 3. Sta.sh

Upload each photo to [Sta.sh](https://sta.sh) (DeviantArt's staging area) before its scheduled date. Copy the resulting `https://sta.sh/xxxxxxxxx` URL into the queue CSV.

> **Important:** Do not publish a Sta.sh item manually before running the workflow — publishing removes the item and breaks the automation.

---

## The Upload Queue (`upload_queue.csv`)

Each row represents one scheduled upload. Open `queue_manager.html` in your browser for a visual view, or edit the CSV directly.

### Column Reference

| Column | Description | Example |
|---|---|---|
| `upload_id` | Unique ID for this entry | `PH-2026-001` |
| `scheduled_date` | Date to publish (`YYYY-MM-DD`) | `2026-03-01` |
| `scheduled_time` | Time of day (informational) | `10:00` |
| `stash_url_nsfw` | Sta.sh URL for the full/NSFW version | `https://sta.sh/0abc123` |
| `stash_url_safe` | Sta.sh URL for the safe-for-work crop (optional) | `https://sta.sh/0xyz789` |
| `title` | Photo title, or `AUTO` to generate from image | `The Delivery Man, Bangkok` |
| `caption` | Description/caption, or `AUTO` | `A candid moment on the streets of Bangkok...` |
| `keywords` | Comma-separated tags, or `AUTO` | `Bangkok, street photography, Fujifilm` |
| `da_nsfw_flag` | Mark as Mature on DeviantArt | `TRUE` / `FALSE` |
| `category_500px` | 500px category name | `Street` |
| `category_35p` | 35photo genre | `Urban` |
| `caption_fb` | Facebook-specific caption (optional, strips external links) | *(leave blank to use main caption)* |
| `platforms` | Comma-separated platforms to publish to | `DA` / `DA,500PX,35P,FB` |
| `status` | Current state of this entry | `Pending` / `Uploaded` / `Skipped` / `Partial` / `Failed` |
| `upload_timestamp` | Filled in automatically after upload | `2026-03-01 15:12:18` |
| `da_deviation_url` | DeviantArt result URL (filled automatically) | `https://www.deviantart.com/flyy1/art/...` |
| `url_500px` | 500px result URL (filled automatically) | |
| `url_35p` | 35photo result URL (filled automatically) | |
| `url_fb` | Facebook post URL (filled automatically) | |
| `notes` | Free-form notes for yourself | `Golden mount, Bangkok. XT-20` |
| `error_log` | Any errors during upload (filled automatically) | |

### Status Values

| Status | Meaning |
|---|---|
| `Pending` | Scheduled, not yet uploaded |
| `Uploaded` | Successfully published to all target platforms |
| `Partial` | Published to some platforms; check `error_log` |
| `Failed` | All platforms failed; check `error_log` |
| `Skipped` | You chose to skip at the approval gate |

---

## AUTO Metadata

Set `title`, `caption`, and/or `keywords` to exactly `AUTO` and Claude will analyze the photo using its vision capabilities and generate:

- **Title** — 3–8 words, evocative, classical art style
- **Caption** — 2–3 sentences, 150–250 characters, fine art voice; mentions the camera when identifiable
- **Keywords** — 30–40 tags for DeviantArt (DA allows up to 60), drawn from eight categories: location, subject, technique, mood/aesthetic, composition, genre, discovery terms, and gear. Multi-word concepts are written without spaces (`longexposure`, `blackandwhite`) because DA splits on spaces and would otherwise break them into useless single words. For 500px and 35photo, a curated shortlist of 10–15 of the most relevant tags is used instead, as those platforms benefit from precision over volume.

You can pre-fill any combination — for example, set a title manually and leave caption/keywords as `AUTO`.

---

## Running the Workflow

### Daily (manual trigger)

Open Cowork and say:

> **run da-upload**

Claude will find today's scheduled entry and walk through the full workflow.

### On-demand (specific entry)

> **publish PH-2026-007 now**

Claude will confirm the scheduled date, then proceed.

### What Claude does step by step

1. **Loads `config.json`** — checks for placeholder values, warns if any remain
2. **Detects the browser machine** — reads the browser's User-Agent via JavaScript and reports whether Chrome is running on Windows or macOS; no guessing or screenshot needed
3. **Reads the queue** — finds today's `Pending` entry; shows upcoming entries if nothing is scheduled today
4. **Re-upload guard** — stops if the entry is already marked `Uploaded` to prevent duplicates
5. **Views the photo in Chrome** — navigates to the Sta.sh URL; confirms it loads
6. **Generates AUTO metadata** — analyses the image; shows proposed title/caption/keywords
7. **Approval gate** — presents three options: Approve & Publish, Edit Metadata, or Skip Today. No uploads begin until you approve
8. **Uploads in order**: 500px → 35photo → Facebook → DeviantArt (always last)
9. **Verifies account on each platform** — compares logged-in username against `config.json`; stops if there's a mismatch
10. **Updates the CSV** — fills in result URLs, timestamp, and final status
11. **Shows a summary** — lists every platform result and the next upcoming entry

---

## Platform Notes

### DeviantArt

- Published via the **Sta.sh web interface** (Edit or Submit button), not the API
- The Cowork VM cannot reach the DeviantArt API due to proxy sandboxing — browser automation is the only viable path
- Publishing **consumes the Sta.sh item** — it will no longer be accessible at the original URL after this step
- This is why DA is always published **last**

### 500px / 35photo / Facebook

- These platforms require a **local file** on your computer (not a Sta.sh URL)
- Save the photo to `photo-queue/temp/{upload_id}_NSFW.jpg` (and `_SAFE.jpg` for Facebook) before running the workflow
- Claude will prompt you if the file is missing
- Facebook uses the `stash_url_safe` version to avoid mature content flags; if `caption_fb` is blank, the main caption is used with external social links stripped

---

## Safety Features

| Feature | What it does |
|---|---|
| **Re-upload guard** | Warns before publishing an entry already marked `Uploaded` |
| **Account verification** | Checks the logged-in username on each platform against `config.json` before touching any form |
| **Approval gate** | Hard stop before any upload begins — nothing is submitted without your explicit go-ahead |
| **Placeholder check** | Stops at startup if `config.json` still has `YOUR_` placeholder values for a platform that's in the queue |
| **DA always last** | Enforced by the workflow order — Sta.sh is preserved until all other platforms are done |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Config not found" | Ensure `config.json` exists in the queue folder and Cowork has the folder selected |
| "Queue not found" | Confirm Google Drive is synced and Cowork is pointing at the right folder |
| Sta.sh shows "Page not found" | The URL may be wrong, or the item was already published to DA manually |
| Wrong account on platform | Log into the correct account in Chrome, then tell Claude to continue |
| Facebook CAPTCHA | Solve it manually in the browser, then tell Claude to continue |
| Platform upload failed | Status will be `Partial`; check `error_log` in the CSV; reset the row to `Pending` to retry |
| Entry already `Uploaded` | Claude will warn you — confirm explicitly if you really want to re-publish |

---

## Adding New Entries

1. Open `queue_manager.html` in your browser, or edit `upload_queue.csv` directly
2. Add a new row with a unique `upload_id` (e.g. `PH-2026-003`), a `scheduled_date`, the Sta.sh URL, and any pre-known metadata
3. Set `status` to `Pending`
4. Upload your photo to Sta.sh and paste the URL into `stash_url_nsfw`
5. On the scheduled date, run `da-upload` in Cowork

---

## Known Limitations

- The Cowork VM has no outbound internet access to DeviantArt's API — all DA interaction is through the Chrome browser
- Local temp files for 500px/35photo/Facebook must be placed manually; the VM cannot download from Sta.sh directly
- Facebook post URLs are not always immediately available after posting
- The workflow runs on whichever machine has the Claude in Chrome extension active. Claude detects this automatically at startup using the browser's User-Agent string (no guessing needed)
- If you need to run the workflow and your primary machine is off, install the extension on a second machine — Claude will detect it and adapt
