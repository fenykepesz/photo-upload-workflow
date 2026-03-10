# Daily Photo Upload Workflow

Automated photo publishing to **DeviantArt**, **500px**, **35photo.pro**, **VK**, **X.com**, **Bluesky**, and **Facebook** using Playwright browser automation. Photos are staged on Sta.sh, scheduled via a CSV queue, and published through `upload.py`.

---

## How It Works

`upload.py` reads the upload queue CSV, finds rows with status `Approved`, downloads the original image from Sta.sh (preserving EXIF), and publishes to each platform listed in the row's `platforms` field. Upload order: 500px first, then 35photo, then VK, then X, then Bluesky, then Facebook, then DA last (since publishing to DA consumes the Sta.sh staging item).

```
Sta.sh (staging) -> upload.py reads queue -> Downloads image with EXIF
-> 500px upload -> 35photo upload -> VK wall post -> X.com post -> Bluesky post -> Facebook post -> DeviantArt publish (last)
-> CSV updated with results
```

---

## Folder Structure

```
photo-upload-workflow/
├── upload.py                  # Main upload automation script
├── upload_queue.csv           # The upload schedule
├── config.json                # Platform accounts and social links
├── requirements.txt           # Python dependencies (playwright)
├── queue_manager.html         # Visual queue manager (open in browser)
├── upload_queue.example.csv   # Example CSV for reference
├── config.example.json        # Example config for reference
├── temp/                      # Temporary downloaded images (auto-cleaned)
└── chrome-profile/            # Playwright browser profile (login cookies)
```

---

## Setup

### 1. Prerequisites

- Python 3.8+
- [Playwright](https://playwright.dev/python/) (`pip install playwright && playwright install chromium`)

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. config.json

Copy `config.example.json` to `config.json` and fill in your usernames:

```json
{
  "accounts": {
    "deviantart": "YOUR_DEVIANTART_USERNAME",
    "500px": "YOUR_500PX_USERNAME"
  },
  "social_links": {
    "web": "yoursite.com/",
    "instagram": "www.instagram.com/you/",
    "500px": "500px.com/you",
    "deviantart": "you.deviantart.com/"
  }
}
```

Social links are appended to photo descriptions on DA and 500px. Only non-empty entries are included.

### 3. Browser Login (first time)

```bash
python upload.py --login
```

This opens a Chromium window with a persistent profile. Log into 500px, then 35photo.pro, then VK, then X.com, then Bluesky, then Facebook, then DeviantArt when prompted. Cookies are saved to `chrome-profile/` and reused on subsequent runs.

### 4. Sta.sh

Upload each photo to [Sta.sh](https://sta.sh) (DeviantArt's staging area) before its scheduled date. Copy the Sta.sh URL into the queue CSV.

> **Important:** Do not publish a Sta.sh item manually before running the script — publishing removes the item and breaks the automation.

---

## The Upload Queue (`upload_queue.csv`)

Each row represents one scheduled upload. Open `queue_manager.html` in your browser for a visual editor, or edit the CSV directly.

### Column Reference

| Column | Description | Example |
|---|---|---|
| `upload_id` | Unique ID | `PH-2026-001` |
| `scheduled_date` | Date to publish (`YYYY-MM-DD`) | `2026-03-01` |
| `scheduled_time` | Time of day (rows are skipped until this time passes) | `10:00` |
| `stash_url_nsfw` | Sta.sh URL for the photo (primary image) | `https://sta.sh/0abc123` |
| `stash_url_safe` | Sta.sh URL for safe crop (used for NSFW photos on Facebook) | |
| `title` | Photo title | `The Delivery Man, Bangkok` |
| `caption` | Description/caption | `A candid moment on the streets...` |
| `keywords` | Comma-separated tags | `Bangkok,street,Fujifilm` |
| `da_nsfw_flag` | Mark as Mature/NSFW (all platforms) | `TRUE` / `FALSE` |
| `category_500px` | 500px category name | `Travel` |
| `category_35p` | 35photo style/category | `City/Architecture` |
| `platforms` | Comma-separated target platforms | `DA,500PX,35P,VK,X,BSKY,FB` |
| `status` | Current state | `Approved` / `Uploaded` / `Partial` / `Failed` |
| `upload_timestamp` | Filled automatically after upload | `2026-03-01 15:12:18` |
| `da_deviation_url` | DA result URL (filled automatically) | |
| `url_500px` | 500px result (filled automatically) | |
| `url_35p` | 35photo result (filled automatically) | |
| `url_vk` | VK wall post URL (filled automatically) | |
| `url_x` | X.com post URL (filled automatically) | |
| `url_bsky` | Bluesky post URL (filled automatically) | |
| `url_fb` | Facebook post URL (filled automatically) | |
| `notes` | Free-form notes | `Golden mount. XT-20` |
| `error_log` | Errors during upload (filled automatically) | |
| `model_name` | Model name (optional) | `Elena` |
| `da_gallery` | DA galleries (comma-separated) | `Featured,Travel` |
| `da_groups` | DA groups (`Group:Folder` format) | `TheSpiritofArt:Featured` |

### Status Values

| Status | Meaning |
|---|---|
| `Pending` | Scheduled, metadata not yet finalized |
| `Approved` | Ready for upload (script picks up these rows) |
| `Uploaded` | Successfully published to all target platforms |
| `Partial` | Some platforms succeeded; check `error_log` |
| `Skipped` | Manually skipped or deferred |
| `Failed` | All platforms failed; check `error_log` |

---

## Usage

### Upload all approved rows

```bash
python upload.py
```

### Upload a specific row (any status)

```bash
python upload.py --row PH-2026-001
```

### Preview what would happen

```bash
python upload.py --dry-run
```

### Fill forms without submitting

```bash
python upload.py --no-submit
```

### Custom CSV or config path

```bash
python upload.py --csv path/to/queue.csv --config path/to/config.json
```

### All flags

| Flag | Description |
|---|---|
| `--row ID` | Upload only this row (bypasses status filter) |
| `--dry-run` | Preview without launching browser |
| `--no-submit` | Fill forms but don't click Submit/Publish |
| `--csv PATH` | Custom CSV path (default: `upload_queue.csv`) |
| `--config PATH` | Custom config path (default: `config.json`) |
| `--profile PATH` | Custom browser profile directory |
| `--login` | Open browser for first-time platform logins |

---

## Platform Details

### DeviantArt

- Published via the **Sta.sh web submission form** (not the API)
- Publishing **consumes the Sta.sh item** — always runs last
- Fills: title, description, tags, galleries, groups, mature flag, NoAI flag
- Disables "Allow free download of source file" automatically

### 500px

- Requires a **file upload** (not a URL) — the script downloads the original image from Sta.sh automatically, preserving EXIF data
- Uses 500px's 3-step upload wizard: file upload, details, publish
- Fills: title, description, category, keywords, NSFW flag
- Downloaded images are stored temporarily in `temp/` and cleaned up after upload

### 35photo.pro

- Requires a **file upload** — reuses the same image downloaded from Sta.sh
- Simple HTML form (not a wizard)
- Fills: style (category), title, description, tags, adult content flag
- Style categories: Female portrait, City/Architecture, Fine Nudes, Portrait

### VK

- Posts a photo to the user's **VK wall** as a new post with a caption
- Uses the browser "Create post" flow: upload photo, write caption, publish
- No API app registration needed — uses existing browser session cookies

### X.com (Twitter)

- Posts a photo with a short text to the user's **X.com timeline**
- Text is composed from the photo **title + up to 5 hashtags** from keywords, respecting the **280-character limit**
- Title and hashtags are separated by a blank line; hyphens are stripped from tags
- Uses the browser compose flow: no API keys or developer account needed

### Bluesky

- Posts a photo with a short text to the user's **Bluesky timeline**
- Text is composed from the photo **title + up to 5 hashtags** from keywords, respecting the **300-character limit**
- Title and hashtags are separated by a blank line; hyphens are stripped from tags
- **NSFW handling:** If a content warning dialog appears automatically, Bluesky's **Nudity** label is selected
- Uses the browser compose flow: no API keys or app passwords needed

### Facebook

- Posts a photo with **model credit + caption** to the user's **personal timeline** (no social links)
- **NSFW safety:** If `da_nsfw_flag` is `TRUE`, only the safe image (`stash_url_safe`) is used — if missing, the upload **fails** (NSFW photos are never uploaded to Facebook)
- For non-NSFW photos, uses the standard `stash_url_nsfw` image
- Uses the browser "Create post" flow: open composer, write caption, attach photo, post
- No API app registration needed — uses existing browser session cookies

---

## DeviantArt Customizations

### Gallery Selection

Set `da_gallery` to a comma-separated list of galleries:

```
Featured,Travel
```

### Group Submissions

Set `da_groups` to comma-separated `Group:Folder` pairs:

```
TheSpiritofArt:Featured,Street-Shooters:Candid
```

### Model Credit

If `model_name` is set (e.g. `Elena`), this is prepended to descriptions (before the caption):

> Model: Elena
> Please respect the model.

### Social Links

Social media links from `config.json` are appended to descriptions on DA and 500px.

---

## Safety Features

| Feature | What it does |
|---|---|
| **Skip already-uploaded** | Skips a platform if its result URL column is already filled |
| **EXIF preservation** | Downloads original files from Sta.sh via the Download menu (not browser image save) |
| **`--no-submit` mode** | Fills all forms without publishing — visual verification |
| **`--dry-run` mode** | Shows what would happen without launching a browser |
| **DA always last** | 500px, 35photo, VK, X, Bluesky, and Facebook run first; Sta.sh is preserved until DA is done |
| **Error screenshots** | Saves `error_*.png` on failure for debugging |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No browser profile found" | Run `python upload.py --login` first |
| "No rows to upload" | Check that rows have `status=Approved` and a supported platform |
| Sta.sh "Page not found" | URL may be wrong, or the item was already published to DA |
| EXIF missing on 500px | Ensure the Sta.sh download works (check for "..." menu on the Sta.sh page) |
| Wrong account on platform | Run `--login` again to log into the correct account |
| Platform upload failed | Status will be `Partial`; check `error_log`; fix and re-run |
| VK "Not logged into VK" | Run `--login` and log into VK when prompted |
| X "Not logged into X" | Run `--login` and log into X.com when prompted |
| BSKY "Not logged into Bluesky" | Run `--login` and log into Bluesky when prompted |
| FB "Not logged into Facebook" | Run `--login` and log into Facebook when prompted |

---

## AI Metadata Generation

The queue manager dashboard includes built-in AI-powered metadata generation using the Anthropic Claude Vision API. Instead of manually writing titles, captions, and keywords, you can generate them directly from the photo.

### Setup

1. Get an API key from [console.anthropic.com](https://console.anthropic.com/)
2. In the queue manager, click the key icon in the header bar
3. Enter your API key — it's stored in your browser's `localStorage`

### How to Use

1. Create or edit an entry and enter a Sta.sh URL
2. Click **Generate with AI** in the Metadata section
3. The AI analyzes the photo thumbnail (loaded via Sta.sh oEmbed) and returns:
   - **3 title options** (Poetic/Artistic, Minimalist, Technical) — click to select
   - **3 caption options** (Poetic/Artistic, Minimalist, Technical) — click to select
   - **30 single-word keywords** — displayed as removable chips
   - **Suggested categories** for 500px and 35photo
4. Click **Apply Selected** to fill the form fields
5. Edit the results as needed before saving

If the Sta.sh thumbnail can't be loaded (e.g., NSFW/private items), a file picker appears as a fallback to select the image from your computer.

### Notes

- Three model options (selectable in Settings): Opus 4.6 (best quality), Sonnet 4.5 (balanced, default), Haiku 4.5 (fastest/cheapest)
- The AI prompt is fully customizable in the Settings dialog (with Reset to Default)
- API calls go directly from your browser to the Anthropic API — no server needed
- Keywords are generated as single words (e.g., `portrait`, `fashion`, `monochrome`)
- Model names autocomplete from previously used names (stored in browser)

---

## Adding New Entries

1. Open `queue_manager.html` in your browser, or edit `upload_queue.csv` directly
2. Add a row with a unique `upload_id`, `scheduled_date`, Sta.sh URL, and metadata
3. Set `status` to `Approved` when ready
4. Run `python upload.py`
