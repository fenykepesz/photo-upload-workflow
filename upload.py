#!/usr/bin/env python3
"""
Playwright-based photo upload automation for DeviantArt, 500px, 35photo, VK, and X.
Reads upload_queue.csv, uploads to platforms listed in each row's 'platforms' field.

Upload order: 500PX → 35P → VK → X → DA (DA consumes the Sta.sh item on publish, so always last).

Usage:
    python upload.py                          # all Approved rows with supported platforms
    python upload.py --row PH-2026-001        # specific row only
    python upload.py --dry-run                # preview what would happen
    python upload.py --no-submit              # fill form but don't submit
    python upload.py --csv path/to/queue.csv  # custom CSV path
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Constants ─────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CSV = SCRIPT_DIR / "upload_queue.csv"
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
BROWSER_PROFILE = SCRIPT_DIR / "chrome-profile"

TAG_LIMIT = 30
TAG_LIMIT_500PX = 15
SUPPORTED_PLATFORMS = {"DA", "500PX", "35P", "VK", "X", "FB", "BSKY"}
TEMP_DIR = SCRIPT_DIR / "temp"

COLS = [
    "upload_id", "scheduled_date", "scheduled_time",
    "stash_url_nsfw", "stash_url_safe", "title", "caption", "keywords",
    "da_nsfw_flag", "category_500px", "category_35p",
    "platforms", "status", "upload_timestamp",
    "da_deviation_url", "url_500px", "url_35p", "url_vk", "url_x", "url_bsky", "url_fb",
    "notes", "error_log", "model_name", "da_gallery", "da_groups", "location_500px",
    "fb_feeling", "fb_tag_people",
    "vk_tag_people", "vk_groups", "vk_group_caption", "vk_groups_result",
]

SOCIAL_LINK_LABELS = {
    "web": "Web",
    "instagram": "IG",
    "500px": "500px",
    "deviantart": "DA",
}


# ── CLI ───────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Upload approved photos to 500px, 35photo, and DeviantArt via Playwright"
    )
    p.add_argument("--row", help="Upload only this upload_id (e.g. PH-2026-001)")
    p.add_argument("--dry-run", action="store_true", help="Preview without launching browser")
    p.add_argument("--no-submit", action="store_true", help="Fill form but don't click Submit")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to upload_queue.csv")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
    p.add_argument("--profile", type=Path, default=BROWSER_PROFILE, help="Browser profile directory")
    p.add_argument("--login", action="store_true", help="Open browser to log into DeviantArt (first-time setup)")
    return p.parse_args()


# ── Config & CSV ──────────────────────────────────────────────
def load_config(path):
    if not path.exists():
        print(f"WARNING: Config file not found: {path}")
        return {"accounts": {}, "social_links": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_queue(path):
    if not path.exists():
        print(f"ERROR: CSV file not found: {path}")
        sys.exit(1)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def save_row_update(csv_path, upload_id, updates):
    """Read CSV, update one row, write back. Crash-safe per-row updates."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    for row in rows:
        if row["upload_id"] == upload_id:
            row.update(updates)
            break

    # Add any new columns from updates that aren't in the CSV yet
    for key in updates:
        if key not in fieldnames:
            fieldnames.append(key)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


# ── Row filtering ─────────────────────────────────────────────
def get_row_platforms(row):
    """Return set of supported platforms for this row."""
    raw = [p.strip().upper() for p in row.get("platforms", "").split(",")]
    return SUPPORTED_PLATFORMS & set(raw)


def filter_rows(rows, target_id=None):
    now = datetime.now()
    targets = []
    for row in rows:
        # Specific row requested
        if target_id:
            if row["upload_id"] != target_id:
                continue
            # Allow any status when explicitly targeting a row
        else:
            if row.get("status", "").strip() != "Approved":
                continue

        # Check scheduled date/time — skip rows not yet due
        sched_date = row.get("scheduled_date", "").strip()
        sched_time = row.get("scheduled_time", "").strip()
        if sched_date:
            try:
                if sched_time:
                    scheduled = datetime.strptime(f"{sched_date} {sched_time}", "%Y-%m-%d %H:%M")
                else:
                    scheduled = datetime.strptime(sched_date, "%Y-%m-%d")
                if scheduled > now:
                    if target_id:
                        print(f"NOTE: {row['upload_id']} is scheduled for {sched_date} {sched_time} — not yet due")
                    continue
            except ValueError:
                pass  # Unparseable date — let it through

        # Must have at least one supported platform
        platforms = get_row_platforms(row)
        if not platforms:
            if target_id:
                print(f"WARNING: {row['upload_id']} has no supported platforms — skipping")
            continue

        # Skip AUTO fields
        auto_fields = [f for f in ("title", "caption", "keywords") if row.get(f, "").strip() == "AUTO"]
        if auto_fields:
            print(f"WARNING: Skipping {row['upload_id']} — {', '.join(auto_fields)} set to AUTO")
            print("  Fill metadata in queue_manager.html first, then re-run.")
            continue

        targets.append(row)

    return targets


# ── Description & Tags ────────────────────────────────────────
def build_description(row, config):
    """Assemble desc_full: model credit + caption + social links."""
    parts = []

    # Model credit (before caption, on its own line)
    model = row.get("model_name", "").strip()
    if model:
        parts.append(f"Model: {model}\n\nPlease respect the model.\n\n")

    parts.append(row.get("caption", "").strip())

    # Social links (DA, 500px, 35photo — not Facebook)
    social = config.get("social_links", {})
    if social:
        links = []
        for key, label in SOCIAL_LINK_LABELS.items():
            url = social.get(key, "").strip()
            if url:
                links.append(f"{label}: {url}")
        if links:
            parts.append("\n\n---\n" + "\n".join(links))

    return "".join(parts)


def build_description_fb(row):
    """Assemble FB caption: model credit + caption (no social links)."""
    parts = []

    model = row.get("model_name", "").strip()
    if model:
        parts.append(f"Model: {model}\n\nPlease respect the model.\n\n")

    parts.append(row.get("caption", "").strip())

    return "".join(parts)


def build_vk_caption(desc_full, vk_tag_people=""):
    """Append VK @mentions at the end of the caption text."""
    text = desc_full
    if vk_tag_people:
        mentions = []
        for handle in vk_tag_people.split(","):
            h = handle.strip()
            if h:
                if not h.startswith("@"):
                    h = f"@{h}"
                mentions.append(h)
        if mentions:
            text = text + "\n\n" + " ".join(mentions)
    return text


def prepare_tags(keywords_str):
    """Parse keywords CSV string, enforce 30-tag limit."""
    tags = [t.strip() for t in keywords_str.split(",") if t.strip()]
    if len(tags) > TAG_LIMIT:
        print(f"  WARNING: {len(tags)} tags found, trimming to {TAG_LIMIT}")
        tags = tags[:TAG_LIMIT]
    return tags


def parse_groups(groups_str):
    """Parse 'Group:Folder,Group2:Folder2' into list of dicts."""
    if not groups_str or not groups_str.strip():
        return []
    result = []
    for pair in groups_str.split(","):
        pair = pair.strip()
        if ":" in pair:
            group, folder = pair.split(":", 1)
            result.append({"group": group.strip(), "folder": folder.strip()})
    return result


# ── Image download from Sta.sh ────────────────────────────────
def download_stash_image(page, stash_url, upload_id):
    """Navigate to Sta.sh URL, click Download in '...' menu to get the original
    file with EXIF intact.  Uses Playwright's native download handling.
    Returns the local file Path, or None on failure."""
    TEMP_DIR.mkdir(exist_ok=True)
    dest = TEMP_DIR / f"{upload_id}.jpg"

    print(f"  Downloading image from Sta.sh: {stash_url}")
    try:
        page.goto(stash_url, wait_until="networkidle", timeout=30000)
    except PlaywrightTimeout:
        page.goto(stash_url, wait_until="domcontentloaded", timeout=15000)

    page.wait_for_timeout(2000)

    # Click "..." menu → "Download" using Playwright's native download capture
    try:
        # Click the "..." button near "Edit or Submit" (position-based matching)
        clicked = page.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                const editBtn = btns.find(b => b.textContent.trim() === 'Edit or Submit');
                if (!editBtn) return 'edit_btn_not_found';
                const editRect = editBtn.getBoundingClientRect();
                let closest = null, closestDist = Infinity;
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text === 'Edit or Submit' || text === 'Sell Deviation' || text.length > 5) continue;
                    const rect = btn.getBoundingClientRect();
                    const dist = Math.abs(rect.top - editRect.top) + Math.abs(rect.right - editRect.right);
                    if (dist < closestDist && rect.width > 0) {
                        closestDist = dist;
                        closest = btn;
                    }
                }
                if (closest) { closest.click(); return 'ok'; }
                return 'no_candidate_found';
            })()
        """)
        if clicked != 'ok':
            print(f"    WARNING: Could not find '...' button: {clicked}")
            return None
        page.wait_for_timeout(1500)

        # Click "Download" in the menu
        dl_el = page.locator('text="Download"').first
        if dl_el.count() == 0:
            print("    WARNING: 'Download' not found in menu")
            page.keyboard.press("Escape")
            return None

        with page.expect_download(timeout=30000) as download_info:
            dl_el.click()
        download = download_info.value
        download.save_as(str(dest))
        print(f"    Downloaded: {dest.name} ({dest.stat().st_size / 1024:.0f} KB)")
        return dest

    except Exception as e:
        print(f"    WARNING: Download failed: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    return None


# ── Dry run ───────────────────────────────────────────────────
def print_dry_run(rows, config):
    print("\nDRY RUN — no browser will be launched")
    print("=" * 60)
    for i, row in enumerate(rows):
        platforms = get_row_platforms(row)
        desc = build_description(row, config)
        tags = prepare_tags(row.get("keywords", ""))
        groups = parse_groups(row.get("da_groups", ""))
        galleries = row.get("da_gallery", "Featured")

        print(f"\nRow {i + 1}: {row['upload_id']}")
        print(f"  Platforms:   {', '.join(sorted(platforms))}")
        print(f"  Sta.sh:      {row.get('stash_url_nsfw', '(missing)')}")
        print(f"  Title:       {row.get('title', '(missing)')}")
        print(f"  Tags ({len(tags)}):   {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}")
        print(f"  NSFW:        {row.get('da_nsfw_flag', 'FALSE')}")
        if "DA" in platforms:
            print(f"  Gallery:     {galleries}")
            if groups:
                print(f"  Groups:      {', '.join(f'{g['group']}:{g['folder']}' for g in groups)}")
        if "500PX" in platforms:
            print(f"  500px cat:   {row.get('category_500px', '(none)')}")
            already = row.get("url_500px", "").strip()
            if already:
                print(f"  500px URL:   {already} (already uploaded — will skip)")
        if "35P" in platforms:
            print(f"  35photo cat: {row.get('category_35p', '(none)')}")
            already = row.get("url_35p", "").strip()
            if already:
                print(f"  35photo URL: {already} (already uploaded — will skip)")
        if "VK" in platforms:
            already = row.get("url_vk", "").strip()
            if already:
                print(f"  VK URL:      {already} (already uploaded — will skip)")
        if "X" in platforms:
            already = row.get("url_x", "").strip()
            if already:
                print(f"  X URL:       {already} (already uploaded — will skip)")
        if "BSKY" in platforms:
            already = row.get("url_bsky", "").strip()
            if already:
                print(f"  BSKY URL:    {already} (already uploaded — will skip)")
        if "FB" in platforms:
            already = row.get("url_fb", "").strip()
            if already:
                print(f"  FB URL:      {already} (already uploaded — will skip)")
        if "DA" in platforms:
            already = row.get("da_deviation_url", "").strip()
            if already:
                print(f"  DA URL:      {already} (already uploaded — will skip)")
        print(f"  Description: {desc[:100]}{'...' if len(desc) > 100 else ''}")

    print("\n" + "=" * 60)
    print(f"Would upload {len(rows)} row(s). Run without --dry-run to proceed.")


# ── 500px Upload ──────────────────────────────────────────────
def upload_to_500px(page, row, desc_full, tags, image_path, no_submit=False):
    """
    Automate 500px photo upload.
    Returns {"success": bool, "url_500px": str, "error": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_500px": "", "error": "No image file available"}

    category = row.get("category_500px", "").strip()

    # ── Navigate to 500px and open upload modal ────────────────
    print("  Opening 500px upload modal...")
    try:
        page.goto("https://500px.com", wait_until="networkidle", timeout=30000)
    except PlaywrightTimeout:
        page.goto("https://500px.com", wait_until="domcontentloaded", timeout=15000)

    page.wait_for_timeout(3000)

    if "login" in page.url.lower() or "sign" in page.url.lower():
        return {"success": False, "url_500px": "",
                "error": "Not logged into 500px — run: python upload.py --login"}

    # Hover Upload button → click dropdown item → modal opens
    upload_btn = page.locator("button:has-text('Upload'), a:has-text('Upload')").first
    upload_btn.hover()
    page.wait_for_timeout(1500)
    try:
        dropdown_item = page.locator('.ant-dropdown a, .ant-dropdown-menu-item').filter(has_text="Upload").first
        dropdown_item.click(timeout=5000)
    except Exception:
        # Fallback: JS click on dropdown item
        page.evaluate("""
            (() => {
                const items = document.querySelectorAll('.ant-dropdown a, .ant-dropdown-menu-item');
                for (const item of items) {
                    if (item.textContent.trim() === 'Upload') { item.click(); return; }
                }
            })()
        """)

    # Wait for the upload modal to appear
    try:
        page.locator('text=Drag files to upload').wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout:
        print("    WARNING: Upload modal not detected — continuing anyway")

    page.wait_for_timeout(1000)

    # ── Upload file ──────────────────────────────────────────
    print(f"  Uploading file...")
    file_input = page.locator('input[type="file"]')
    if file_input.count() > 0:
        file_input.first.set_input_files(str(image_path))
    else:
        try:
            page.locator('button:has-text("Add photos")').click(timeout=5000)
            page.wait_for_timeout(1000)
            file_input = page.locator('input[type="file"]')
            if file_input.count() > 0:
                file_input.first.set_input_files(str(image_path))
            else:
                return {"success": False, "url_500px": "", "error": "No file input found on 500px upload page"}
        except Exception as e:
            return {"success": False, "url_500px": "", "error": f"No file input: {e}"}

    # Wait for upload to process (500px extracts EXIF server-side)
    page.wait_for_timeout(30000)

    # ── Fill metadata fields ────────────────────────────────
    title = row.get("title", "").strip()
    nsfw = row.get("da_nsfw_flag", "FALSE").strip().upper() == "TRUE"

    def js_escape_500px(s):
        return s.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")

    # Title — React controlled input, use native setter + events
    print(f"  Setting metadata: title, description, category, keywords...")
    page.evaluate(f"""
        (() => {{
            const el = document.querySelector('#editpanel-title');
            if (!el) return;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, '{js_escape_500px(title)}');
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.dispatchEvent(new Event('blur', {{bubbles: true}}));
        }})()
    """)
    page.wait_for_timeout(500)

    # Description — React controlled textarea, use native setter + events
    page.evaluate(f"""
        (() => {{
            const el = document.querySelector('#edit-panel-description');
            if (!el) return;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            setter.call(el, `{js_escape_500px(desc_full)}`);
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.dispatchEvent(new Event('blur', {{bubbles: true}}));
        }})()
    """)
    page.wait_for_timeout(500)

    # Category — use JS click to bypass overlay (image preview intercepts pointer events)
    if category:
        page.click('#category-input')
        page.wait_for_timeout(500)
        try:
            # Get position of the dropdown to hover over it for scrolling
            first_opt = page.locator('[class*="DropdownOption"], [role="option"]').first
            box = first_opt.bounding_box()
            selected = False
            if box:
                # Hover over the dropdown area
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                for _scroll in range(25):
                    result = page.evaluate("""(cat) => {
                        const options = document.querySelectorAll('[class*="DropdownOption"], [role="option"]');
                        for (const item of options) {
                            if (item.textContent.trim() === cat) { item.click(); return true; }
                        }
                        return false;
                    }""", category)
                    if result:
                        selected = True
                        break
                    # Mouse wheel scroll down over the dropdown
                    page.mouse.wheel(0, 120)
                    page.wait_for_timeout(200)
            if not selected:
                print(f"    WARNING: Could not select category '{category}' via JS")
        except Exception as e:
            print(f"    WARNING: Could not select category '{category}': {e}")
        page.wait_for_timeout(300)

    # Keywords
    keywords_input = page.locator('#editpanel-keywords')
    keywords_input.scroll_into_view_if_needed()
    keywords_input.click()
    page.wait_for_timeout(300)
    for tag in tags:
        page.keyboard.type(tag, delay=50)
        page.keyboard.press('Enter')
        page.wait_for_timeout(300)

    # NSFW
    if nsfw:
        try:
            nsfw_toggle = page.locator('[class*="nsfw"], [class*="safe"], label:has-text("NSFW"), label:has-text("Not Safe")').first
            nsfw_toggle.click(timeout=3000)
        except Exception as e:
            print(f"    WARNING: Could not find NSFW toggle: {e}")

    # Location — skip if EXIF already populated or no location in CSV
    location = row.get("location_500px", "").strip()
    if location:
        try:
            has_location = page.evaluate("""() => {
                const el = document.querySelector('input[placeholder*="Location"]');
                return el ? el.value.trim() : '';
            }""")
            if not has_location:
                print(f"  Setting location: {location}")
                loc_input = page.locator('input[placeholder*="Location"]')
                loc_input.scroll_into_view_if_needed()
                loc_input.click()
                page.wait_for_timeout(300)
                page.keyboard.type(location, delay=50)
                page.wait_for_timeout(5000)  # wait for autocomplete suggestions
                # Click the first suggestion containing the query text
                clicked = page.evaluate(r"""(query) => {
                    const input = document.querySelector('input[placeholder*="Location"]');
                    if (!input) return {ok: false, reason: 'no input'};
                    const rect = input.getBoundingClientRect();
                    // Split query into words for flexible matching
                    // "San Francisco, USA" matches "San Francisco, CA, USA" (all words present)
                    const words = query.toLowerCase().replace(/[,-]/g, ' ').split(/\s+/).filter(w => w.length > 0);
                    const matchesQuery = (text) => {
                        const lt = text.toLowerCase();
                        return words.every(w => lt.includes(w));
                    };
                    // Search broadly — location suggestions use different components than category
                    const all = document.querySelectorAll('div, li, a, span');
                    for (const el of all) {
                        const r = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        if (r.top >= rect.bottom + 2 && r.top < rect.bottom + 400
                            && r.height > 20 && r.height < 80 && r.width > 100
                            && matchesQuery(text)
                            && el.children.length <= 3) {
                            el.click();
                            return {ok: true, text: text.substring(0, 60)};
                        }
                    }
                    return {ok: false, reason: 'no matching suggestions', inputVal: input.value};
                }""", location)
                if clicked.get("ok"):
                    print(f"    Selected: {clicked.get('text')}")
                else:
                    print(f"    WARNING: No location suggestion found ({clicked.get('reason')})")
                page.wait_for_timeout(5000)
            else:
                print(f"  Location already set (EXIF): {has_location}")
        except Exception as e:
            print(f"    WARNING: Could not set location: {e}")

    page.wait_for_timeout(1000)

    if no_submit:
        print("  --no-submit: skipping publish")
        return {"success": True, "url_500px": "NO_SUBMIT", "error": ""}

    # Advance wizard: Details → Additional info → Upload
    print("  Publishing...")
    next_btn = page.locator('button:has-text("Next")').first
    if next_btn.count() > 0:
        next_btn.click()
        page.wait_for_timeout(4000)

    # Click final Upload button (.last to avoid nav bar match)
    publish_clicked = False
    for label in ("Publish", "Submit", "Save", "Post", "Done"):
        btn = page.locator(f'button:has-text("{label}")').first
        if btn.count() > 0:
            btn.click()
            publish_clicked = True
            break

    if not publish_clicked:
        upload_btns = page.locator('button:has-text("Upload")')
        if upload_btns.count() > 1:
            upload_btns.last.click()
            publish_clicked = True
        elif upload_btns.count() == 1:
            upload_btns.first.click()
            publish_clicked = True

    if not publish_clicked:
        return {"success": False, "url_500px": "", "error": "No Publish button found"}

    page.wait_for_timeout(5000)
    print("  Upload complete")
    return {"success": True, "url_500px": "UPLOADED", "error": ""}


# ── 35photo Upload ────────────────────────────────────────────
def upload_to_35photo(page, row, desc_full, tags, image_path, no_submit=False):
    """
    Automate 35photo.pro photo upload.
    Returns {"success": bool, "url_35p": str, "error": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_35p": "", "error": "No image file available"}

    category = row.get("category_35p", "").strip()
    title = row.get("title", "").strip()
    nsfw = row.get("da_nsfw_flag", "FALSE").strip().upper() == "TRUE"

    # ── Navigate to 35photo upload page ──────────────────────
    print("  Opening 35photo upload page...")
    try:
        page.goto("https://35photo.pro", wait_until="networkidle", timeout=30000)
    except PlaywrightTimeout:
        page.goto("https://35photo.pro", wait_until="domcontentloaded", timeout=15000)

    page.wait_for_timeout(2000)

    if "login" in page.url.lower():
        return {"success": False, "url_35p": "",
                "error": "Not logged into 35photo — run: python upload.py --login"}

    # Click the "Upload" button in top nav
    try:
        upload_link = page.locator('a:has-text("Upload")').first
        upload_link.click(timeout=5000)
    except Exception:
        # Fallback: navigate directly
        page.goto("https://35photo.pro/upload/", wait_until="networkidle", timeout=30000)

    page.wait_for_timeout(3000)

    # ── Upload file ──────────────────────────────────────────
    print(f"  Uploading file...")
    file_input = page.locator('input[type="file"]')
    if file_input.count() > 0:
        file_input.first.set_input_files(str(image_path))
    else:
        return {"success": False, "url_35p": "", "error": "No file input found on 35photo upload page"}

    # Wait for upload to process and form to appear
    print("  Waiting for upload to complete...")
    page.wait_for_timeout(30000)

    # Scroll down to make the metadata form visible
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    # ── Fill metadata fields ────────────────────────────────
    print(f"  Setting metadata: style, title, description, tags...")

    # Style (category) — standard HTML <select>, skip the time interval select
    if category:
        try:
            style_select = page.locator('select.photoData, select:not(#allowTimeInteval)').first
            style_select.scroll_into_view_if_needed()
            style_select.select_option(label=category, timeout=5000)
        except Exception as e:
            print(f"    WARNING: Could not select style '{category}': {e}")

    page.wait_for_timeout(300)

    # Title — standard HTML input with class photoData
    try:
        title_input = page.locator('input.photoData[placeholder="Title"], input[placeholder="Title"]').first
        title_input.scroll_into_view_if_needed()
        title_input.fill(title, timeout=5000)
    except Exception as e:
        print(f"    WARNING: Could not fill title: {e}")

    page.wait_for_timeout(300)

    # Description — standard HTML textarea
    try:
        desc_input = page.locator('textarea.photoData[placeholder="Description"], textarea[placeholder="Description"]').first
        desc_input.scroll_into_view_if_needed()
        desc_input.fill(desc_full, timeout=5000)
    except Exception as e:
        print(f"    WARNING: Could not fill description: {e}")

    page.wait_for_timeout(300)

    # Tags — the real <input var-input="photo_keywords"> has display:none.
    # 35photo wraps it with a custom chip UI: div.upload-tags-editor > input.upload-tags-input.
    # Click the visible input directly, or fall back to clicking the editor container.
    try:
        tag_input = page.locator('input.upload-tags-input')
        if tag_input.count() > 0:
            tag_input.first.scroll_into_view_if_needed()
            tag_input.first.click(timeout=5000)
        else:
            # Fallback: click the editor container to activate the input
            editor = page.locator('.upload-tags-editor')
            if editor.count() > 0:
                editor.first.scroll_into_view_if_needed()
                editor.first.click(timeout=5000)
            else:
                # Last resort: click parent of the hidden input
                parent_loc = page.locator('input[var-input="photo_keywords"]').locator('..')
                parent_loc.scroll_into_view_if_needed()
                parent_loc.click(timeout=5000)
        page.wait_for_timeout(500)
        for tag in tags:
            page.keyboard.type(tag, delay=50)
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)
    except Exception as e:
        print(f"    WARNING: Could not fill tags: {e}")

    # Background — try to select black (last swatch)
    try:
        bg_swatches = page.locator('[class*="background"] span, [class*="bg"] span, .color-swatch')
        if bg_swatches.count() > 0:
            bg_swatches.last.click(timeout=2000)
    except Exception:
        pass  # Not critical

    # Adult content checkbox — use JS to find and check it directly
    if nsfw:
        print("  Setting adult content flag...")
        checked = page.evaluate("""
            (() => {
                // Find all checkboxes on the page
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                for (const cb of checkboxes) {
                    // Check if this checkbox or its parent/label contains "Adult content" text
                    const parent = cb.closest('label, div, span, td') || cb.parentElement;
                    const nearby = parent ? parent.textContent : '';
                    if (nearby.includes('Adult content') || nearby.includes('adult content')) {
                        if (!cb.checked) {
                            cb.click();
                            return 'checked';
                        }
                        return 'already_checked';
                    }
                }
                // Fallback: look for any element with "Adult content 18" text and click it
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    if (el.children.length === 0 && el.textContent.includes('Adult content 18')) {
                        el.click();
                        return 'clicked_text';
                    }
                }
                return 'not_found';
            })()
        """)
        print(f"    Adult content: {checked}")

    page.wait_for_timeout(1000)

    if no_submit:
        print("  --no-submit: skipping publish")
        return {"success": True, "url_35p": "NO_SUBMIT", "error": ""}

    # ── Publish ──────────────────────────────────────────────
    print("  Publishing...")
    publish_clicked = False
    for label in ("Publish", "Submit", "Save", "Upload photo", "Upload"):
        btn = page.locator(f'button:has-text("{label}"), input[type="submit"][value="{label}"], a:has-text("{label}")')
        if btn.count() > 0:
            btn.first.click()
            publish_clicked = True
            break

    if not publish_clicked:
        return {"success": False, "url_35p": "", "error": "No Publish button found"}

    page.wait_for_timeout(5000)
    print("  Upload complete")
    return {"success": True, "url_35p": "UPLOADED", "error": ""}


# ── VK Upload (Playwright) ────────────────────────────────────
def upload_to_vk(page, desc_full, image_path, vk_tag_people="", vk_groups="", vk_group_caption="", no_submit=False):
    """
    Post a photo to the VK wall via browser automation, then suggest to VK groups.
    Flow: feed → Create post → write caption → upload photo → Next → Publish → groups.
    Returns {"success": bool, "url_vk": str, "error": str, "vk_groups_result": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_vk": "", "error": "No image file available"}

    # Navigate to VK feed
    print("  Opening VK feed...")
    try:
        page.goto("https://vk.com/feed", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return {"success": False, "url_vk": "", "error": "Timeout loading VK feed"}
    page.wait_for_timeout(3000)

    # Check if logged in (VK redirects to login page if not)
    if "/login" in page.url or "/authorize" in page.url:
        return {"success": False, "url_vk": "", "error": "Not logged into VK — run --login first"}

    # Click "Create post" button
    print("  Creating new post...")
    try:
        create_btn = page.locator('text="Create post"').first
        create_btn.click(timeout=5000)
    except Exception:
        try:
            create_btn = page.locator('[class*="create"], [class*="new_post"], [data-testid*="post"]').first
            create_btn.click(timeout=5000)
        except Exception:
            return {"success": False, "url_vk": "", "error": "Could not find 'Create post' button"}
    page.wait_for_timeout(2000)

    # Handle "saved draft" dialog — click "Start over" if it appears
    try:
        start_over = page.locator('text="Start over"')
        if start_over.count() > 0 and start_over.first.is_visible():
            print("    Draft dialog found — clicking 'Start over'")
            start_over.first.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass

    # Fill caption FIRST (before photo upload — the caption area is simpler before photo is attached)
    print("  Writing caption...")
    try:
        caption_ok = page.evaluate("""(text) => {
            const vh = window.innerHeight;
            // Find contenteditable elements within the visible viewport
            const candidates = document.querySelectorAll(
                '[contenteditable="true"], [role="textbox"]'
            );
            let best = null;
            let bestArea = 0;
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                // Must be visible, reasonably sized, and within the viewport
                if (r.width > 100 && r.height > 15 && r.top > 50 && r.top < vh) {
                    const area = r.width * r.height;
                    if (area > bestArea) {
                        best = el;
                        bestArea = area;
                    }
                }
            }
            if (best) {
                best.focus();
                best.click();
                // Use execCommand to insert text (works with contenteditable)
                document.execCommand('insertText', false, text);
                return {found: true, tag: best.tagName, class: best.className?.substring(0, 80),
                        rect: {top: best.getBoundingClientRect().top,
                               width: best.getBoundingClientRect().width}};
            }
            return {found: false};
        }""", desc_full)
        print(f"    Caption area: {caption_ok}")
        if not caption_ok.get("found"):
            # Fallback: click placeholder text directly, then type
            placeholder = page.locator('text="Write something here..."').first
            placeholder.click(timeout=3000)
            page.wait_for_timeout(300)
            page.keyboard.type(desc_full, delay=10)
    except Exception as e:
        print(f"    WARNING: Could not fill caption: {e}")

    page.wait_for_timeout(1000)

    # @mention tagging — type each @handle and click the autocomplete suggestion
    if vk_tag_people:
        mentions = [h.strip() for h in vk_tag_people.split(",") if h.strip()]
        if mentions:
            print(f"  Tagging {len(mentions)} people via @mentions...")
            for mention in mentions:
                handle = mention if mention.startswith("@") else f"@{mention}"
                print(f"    Typing: {handle}")
                # Press Enter to start a new line, then type the @mention
                page.keyboard.press("Enter")
                page.keyboard.type(handle, delay=50)
                page.wait_for_timeout(3000)

                # Click the first suggestion in the autocomplete dropdown.
                # VK shows a dropdown with rows like "Naya Mamedova @nayamodel".
                # Use the same simple locator pattern as "Create post" — text= matching.
                bare = mention.lstrip("@")
                try:
                    suggestion = page.locator(f'text="@{bare}"').first
                    suggestion.click(timeout=5000)
                    print(f"    Selected suggestion for @{bare}")
                except Exception:
                    print(f"    WARNING: No autocomplete suggestion found for @{bare}")
                page.wait_for_timeout(1000)

    # Upload file — use expect_file_chooser to intercept native file picker
    print("  Uploading photo...")
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            # Click "Upload from device" to trigger the native file picker
            upload_btn = page.locator('text="Upload from device"').first
            upload_btn.click(timeout=3000)
        file_chooser = fc_info.value
        file_chooser.set_files(image_path)
        print(f"    Photo file set via file chooser")
    except Exception as e:
        print(f"    File chooser approach failed: {e}")
        # Fallback: try setting file input directly
        file_input = page.locator('input[type="file"]')
        if file_input.count() > 0:
            file_input.last.set_input_files(image_path)
            print(f"    Fallback: file set on input directly")
        else:
            return {"success": False, "url_vk": "", "error": "Could not upload file"}

    # Wait for photo to process
    print("  Waiting for photo to process...")
    page.wait_for_timeout(30000)

    # Click "Next" button
    print("  Clicking Next...")
    try:
        next_btn = page.locator('button:has-text("Next"), [class*="next"]').first
        next_btn.click(timeout=5000)
    except Exception:
        return {"success": False, "url_vk": "", "error": "Could not find Next button"}
    page.wait_for_timeout(2000)

    if no_submit:
        print("  --no-submit: skipping publish")
        wall_url = "NO_SUBMIT"
    else:
        # Click "Publish" button
        print("  Publishing...")
        try:
            pub_btn = page.locator('button:has-text("Publish"), [class*="publish"]').first
            pub_btn.click(timeout=5000)
        except Exception:
            return {"success": False, "url_vk": "", "error": "Could not find Publish button"}
        page.wait_for_timeout(5000)
        print("  Wall post created")
        wall_url = "UPLOADED"

    wall_result = {"success": True, "url_vk": wall_url, "error": "", "vk_groups_result": ""}

    # ── VK Group Posting ──
    if not vk_groups or not vk_groups.strip():
        return wall_result

    group_slugs = [g.strip() for g in vk_groups.split(",") if g.strip()]
    if not group_slugs:
        return wall_result

    # Build group caption (without @mentions — those are handled via autocomplete in the group function)
    group_caption_text = vk_group_caption.strip() if vk_group_caption else desc_full

    group_results = []
    for slug in group_slugs:
        print(f"\n  ── VK Group: {slug} ──")
        try:
            gr = suggest_post_to_vk_group(page, slug, group_caption_text, image_path, vk_tag_people, no_submit)
        except Exception as e:
            gr = {"success": False, "error": f"Unexpected: {e}"}

        status = "OK" if gr["success"] else "FAILED"
        group_results.append(f"{slug}:{status}")

        if gr["success"]:
            print(f"    Group {slug}: OK")
        else:
            print(f"    Group {slug}: FAILED — {gr.get('error', 'unknown')}")

    wall_result["vk_groups_result"] = ",".join(group_results)

    failed_groups = [r for r in group_results if "FAILED" in r]
    if failed_groups:
        wall_result["error"] = f"Wall OK but {len(failed_groups)} group(s) failed"

    ok_count = len(group_results) - len(failed_groups)
    print(f"\n  VK Groups: {ok_count}/{len(group_results)} succeeded")
    return wall_result


def suggest_post_to_vk_group(page, group_slug, caption, image_path, vk_tag_people="", no_submit=False):
    """
    Suggest a post to a VK group (community).
    Flow: group page → Suggest post → write caption → @mentions → upload photo → Next → Submit.
    Returns {"success": bool, "error": str}
    """
    group_url = f"https://vk.com/{group_slug}"
    print(f"    Opening group: {group_url}")
    try:
        page.goto(group_url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return {"success": False, "error": f"Timeout loading {group_url}"}
    page.wait_for_timeout(3000)

    # Check the page loaded (not a 404 or redirect)
    if "/blank" in page.url or "/404" in page.url:
        return {"success": False, "error": f"Group not found: {group_slug}"}

    # Click "Suggest post" button — same pattern as "Create post" on the feed page
    print(f"    Clicking 'Suggest post'...")
    try:
        suggest_btn = page.locator('text="Suggest post"').first
        suggest_btn.click(timeout=5000)
    except Exception:
        try:
            suggest_btn = page.locator('text="Suggest a Post"').first
            suggest_btn.click(timeout=5000)
        except Exception:
            try:
                suggest_btn = page.locator('text="Suggest a post"').first
                suggest_btn.click(timeout=5000)
            except Exception:
                return {"success": False, "error": f"Could not find 'Suggest post' button in {group_slug}"}
    page.wait_for_timeout(2000)

    # Handle draft dialog (same as wall post)
    try:
        start_over = page.locator('text="Start over"')
        if start_over.count() > 0 and start_over.first.is_visible():
            print("      Draft dialog — clicking 'Start over'")
            start_over.first.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass

    # Write caption (same execCommand pattern as wall post)
    print(f"    Writing group caption...")
    try:
        caption_ok = page.evaluate("""(text) => {
            const vh = window.innerHeight;
            const candidates = document.querySelectorAll(
                '[contenteditable="true"], [role="textbox"]'
            );
            let best = null;
            let bestArea = 0;
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                if (r.width > 100 && r.height > 15 && r.top > 50 && r.top < vh) {
                    const area = r.width * r.height;
                    if (area > bestArea) {
                        best = el;
                        bestArea = area;
                    }
                }
            }
            if (best) {
                best.focus();
                best.click();
                document.execCommand('insertText', false, text);
                return {found: true, tag: best.tagName, class: best.className?.substring(0, 80),
                        rect: {top: best.getBoundingClientRect().top,
                               width: best.getBoundingClientRect().width}};
            }
            return {found: false};
        }""", caption)
        print(f"      Caption area: {caption_ok}")
        if not caption_ok.get("found"):
            placeholder = page.locator('text="Write something here..."').first
            placeholder.click(timeout=3000)
            page.wait_for_timeout(300)
            page.keyboard.type(caption, delay=10)
    except Exception as e:
        print(f"      WARNING: Could not fill caption: {e}")

    page.wait_for_timeout(1000)

    # @mention tagging — same autocomplete click approach as wall post
    if vk_tag_people:
        mentions = [h.strip() for h in vk_tag_people.split(",") if h.strip()]
        for mention in mentions:
            handle = mention if mention.startswith("@") else f"@{mention}"
            print(f"      Typing mention: {handle}")
            page.keyboard.press("Enter")
            page.keyboard.type(handle, delay=50)
            page.wait_for_timeout(3000)

            bare = mention.lstrip("@")
            try:
                suggestion = page.locator(f'text="@{bare}"').first
                suggestion.click(timeout=5000)
                print(f"      Selected suggestion for @{bare}")
            except Exception:
                print(f"      WARNING: No autocomplete suggestion found for @{bare}")
            page.wait_for_timeout(1000)

    page.wait_for_timeout(1000)

    # Upload photo (same file chooser pattern)
    print(f"    Uploading photo to group...")
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            upload_btn = page.locator('text="Upload from device"').first
            upload_btn.click(timeout=3000)
        file_chooser = fc_info.value
        file_chooser.set_files(image_path)
        print(f"      Photo file set via file chooser")
    except Exception as e:
        print(f"      File chooser approach failed: {e}")
        file_input = page.locator('input[type="file"]')
        if file_input.count() > 0:
            file_input.last.set_input_files(image_path)
            print(f"      Fallback: file set on input directly")
        else:
            return {"success": False, "error": f"Could not upload file to {group_slug}"}

    # Wait for processing
    print(f"    Waiting for photo processing...")
    page.wait_for_timeout(30000)

    # Click Next
    print(f"    Clicking Next...")
    try:
        next_btn = page.locator('button:has-text("Next"), [class*="next"]').first
        next_btn.click(timeout=5000)
    except Exception:
        return {"success": False, "error": f"Could not find Next button in {group_slug}"}
    page.wait_for_timeout(2000)

    if no_submit:
        print(f"    --no-submit: skipping suggest")
        return {"success": True, "error": ""}

    # Click Submit/Suggest/Publish
    print(f"    Submitting suggestion...")
    try:
        submit_btn = page.locator(
            'button:has-text("Submit"), button:has-text("Suggest"), '
            'button:has-text("Publish"), [class*="submit"], [class*="publish"]'
        ).first
        submit_btn.click(timeout=5000)
    except Exception:
        return {"success": False, "error": f"Could not find Submit button in {group_slug}"}

    page.wait_for_timeout(5000)
    print(f"    Suggested to {group_slug}")
    return {"success": True, "error": ""}


# ── X.com Upload (Playwright) ─────────────────────────────────
X_CHAR_LIMIT = 280
BSKY_CHAR_LIMIT = 300


def build_x_post_text(title, keywords_str, max_tags=5):
    """Build a tweet from title + hashtags, fitting within 280 characters.
    Title and hashtags are separated by a blank line. Hyphens are stripped from tags.
    Limited to max_tags hashtags."""
    text = title.strip()
    if not keywords_str:
        return text[:X_CHAR_LIMIT]
    tags = [k.strip() for k in keywords_str.split(",") if k.strip()]
    hashtags = ""
    tag_count = 0
    for tag in tags:
        if tag_count >= max_tags:
            break
        hashtag = "#" + tag.replace(" ", "").replace("-", "")
        sep = " " if hashtags else ""
        candidate_tags = hashtags + sep + hashtag
        full = text + "\n\n" + candidate_tags
        if len(full) <= X_CHAR_LIMIT:
            hashtags = candidate_tags
            tag_count += 1
        else:
            break
    if hashtags:
        text = text + "\n\n" + hashtags
    return text


def build_bsky_post_text(title, keywords_str, max_tags=5):
    """Build a Bluesky post from title + hashtags, fitting within 300 characters.
    Title and hashtags are separated by a blank line. Hyphens are stripped from tags.
    Limited to max_tags hashtags."""
    text = title.strip()
    if not keywords_str:
        return text[:BSKY_CHAR_LIMIT]
    tags = [k.strip() for k in keywords_str.split(",") if k.strip()]
    hashtags = ""
    tag_count = 0
    for tag in tags:
        if tag_count >= max_tags:
            break
        hashtag = "#" + tag.replace(" ", "").replace("-", "")
        sep = " " if hashtags else ""
        candidate_tags = hashtags + sep + hashtag
        full = text + "\n\n" + candidate_tags
        if len(full) <= BSKY_CHAR_LIMIT:
            hashtags = candidate_tags
            tag_count += 1
        else:
            break
    if hashtags:
        text = text + "\n\n" + hashtags
    return text


def upload_to_x(page, post_text, image_path, no_submit=False):
    """
    Post a photo with text to X.com via browser automation.
    Flow: home → compose → write text → attach photo → Post.
    Returns {"success": bool, "url_x": str, "error": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_x": "", "error": "No image file available"}

    # Navigate to X compose page
    print("  Opening X.com compose...")
    try:
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return {"success": False, "url_x": "", "error": "Timeout loading X.com"}
    page.wait_for_timeout(3000)

    # Check if logged in
    if "/login" in page.url or "/i/flow/login" in page.url:
        return {"success": False, "url_x": "", "error": "Not logged into X — run --login first"}

    # Write post text — find the tweet composer contenteditable
    # Use simulated paste (ClipboardEvent) instead of execCommand('insertText')
    # because DraftJS duplicates text when insertText contains newlines.
    print("  Writing post text...")
    try:
        caption_ok = page.evaluate("""(text) => {
            const vh = window.innerHeight;
            const candidates = document.querySelectorAll(
                '[contenteditable="true"], [role="textbox"]'
            );
            let best = null;
            let bestArea = 0;
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                if (r.width > 100 && r.height > 15 && r.top > 50 && r.top < vh) {
                    const area = r.width * r.height;
                    if (area > bestArea) {
                        best = el;
                        bestArea = area;
                    }
                }
            }
            if (best) {
                best.focus();
                best.click();
                // Simulate paste event — DraftJS handles paste correctly
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const evt = new ClipboardEvent('paste', {
                    clipboardData: dt, bubbles: true, cancelable: true
                });
                best.dispatchEvent(evt);
                return {found: true, tag: best.tagName, class: best.className?.substring(0, 80),
                        rect: {top: best.getBoundingClientRect().top,
                               width: best.getBoundingClientRect().width}};
            }
            return {found: false};
        }""", post_text)
        print(f"    Compose area: {caption_ok}")
        if not caption_ok.get("found"):
            # Fallback: click placeholder text
            placeholder = page.locator('text="What\'s happening?"').first
            placeholder.click(timeout=3000)
            page.wait_for_timeout(300)
            page.keyboard.type(post_text, delay=10)
    except Exception as e:
        print(f"    WARNING: Could not write text: {e}")

    page.wait_for_timeout(1000)

    # Upload photo — click the media button to trigger file picker
    print("  Uploading photo...")
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            # The media button is an input[type=file] or a button with media icon
            media_input = page.locator('input[type="file"][accept*="image"]')
            if media_input.count() > 0:
                media_input.first.set_input_files(image_path)
                # Cancel the expect_file_chooser since we used set_input_files
                raise Exception("used set_input_files directly")
            else:
                # Click the media/photo button (camera/image icon)
                media_btn = page.locator('[aria-label="Add photos or video"], [data-testid="fileInput"]').first
                media_btn.click(timeout=3000)
        file_chooser = fc_info.value
        file_chooser.set_files(image_path)
        print(f"    Photo attached via file chooser")
    except Exception as e:
        if "used set_input_files" in str(e):
            print(f"    Photo attached via input element")
        else:
            # Last fallback: try any file input
            file_input = page.locator('input[type="file"]')
            if file_input.count() > 0:
                file_input.first.set_input_files(image_path)
                print(f"    Fallback: photo set on input directly")
            else:
                return {"success": False, "url_x": "", "error": f"Could not attach photo: {e}"}

    # Wait for photo to process
    print("  Waiting for photo to process...")
    page.wait_for_timeout(5000)

    if no_submit:
        print("  --no-submit: skipping post")
        return {"success": True, "url_x": "NO_SUBMIT", "error": ""}

    # Dismiss any hashtag autocomplete dropdown before clicking Post
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # Click "Post" button
    print("  Posting...")
    try:
        post_btn = page.locator('[data-testid="tweetButton"]')
        if post_btn.count() > 0:
            post_btn.first.click(timeout=5000)
        else:
            # Fallback: find button with "Post" text
            post_btn = page.locator('button:has-text("Post")').last
            post_btn.click(timeout=5000)
    except Exception:
        return {"success": False, "url_x": "", "error": "Could not find Post button"}

    page.wait_for_timeout(5000)
    print("  Tweet posted")
    return {"success": True, "url_x": "UPLOADED", "error": ""}


# ── Bluesky Upload ──────────────────────────────────────────────
def upload_to_bsky(page, post_text, image_path, is_nsfw=False, no_submit=False):
    """
    Post a photo with text to Bluesky via browser automation.
    Flow: home → New Post → write text → attach photo → (NSFW label) → Post.
    Returns {"success": bool, "url_bsky": str, "error": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_bsky": "", "error": "No image file available"}

    # Navigate to Bluesky
    print("  Opening Bluesky...")
    try:
        page.goto("https://bsky.app/", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return {"success": False, "url_bsky": "", "error": "Timeout loading Bluesky"}
    page.wait_for_timeout(3000)

    # Check if logged in — Bluesky redirects to login or shows sign-in buttons
    if "/login" in page.url or page.locator('button:has-text("Sign in")').count() > 0:
        return {"success": False, "url_bsky": "", "error": "Not logged into Bluesky — run --login first"}

    # Open composer — click "New Post" button
    print("  Opening composer...")
    try:
        new_post_btn = page.locator(
            '[aria-label="New Post"], '
            '[aria-label="New post"], '
            '[aria-label="Compose post"], '
            '[data-testid="composePostButton"], '
            'button:has-text("New Post"), '
            'a:has-text("New Post")'
        ).first
        new_post_btn.click(timeout=5000)
        page.wait_for_timeout(2000)
    except Exception as e:
        # Diagnostic: list all buttons/links with text or aria-labels
        btns = page.evaluate("""() => {
            const els = document.querySelectorAll('button, a, [role="button"], [role="link"]');
            return Array.from(els)
                .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .map(el => ({
                    tag: el.tagName,
                    label: el.getAttribute('aria-label') || '',
                    text: el.textContent?.trim().substring(0, 40) || '',
                    testid: el.getAttribute('data-testid') || '',
                }))
                .filter(el => el.label || el.text || el.testid)
                .slice(0, 25);
        }""")
        print(f"    Available elements: {btns}")
        return {"success": False, "url_bsky": "", "error": f"Could not open composer: {e}"}

    # Write post text
    print("  Writing post text...")
    try:
        # Bluesky's compose area — try contenteditable with placeholder
        caption_ok = page.evaluate("""(text) => {
            const vh = window.innerHeight;
            const candidates = document.querySelectorAll(
                '[contenteditable="true"], [role="textbox"]'
            );
            let best = null;
            let bestArea = 0;
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                if (r.width > 100 && r.height > 15 && r.top > 30 && r.top < vh) {
                    const area = r.width * r.height;
                    if (area > bestArea) {
                        best = el;
                        bestArea = area;
                    }
                }
            }
            if (best) {
                best.focus();
                best.click();
                document.execCommand('insertText', false, text);
                return {found: true};
            }
            return {found: false};
        }""", post_text)
        if not caption_ok.get("found"):
            # Fallback: click placeholder and type
            placeholder = page.locator('text="What\'s up?"').first
            placeholder.click(timeout=3000)
            page.wait_for_timeout(300)
            page.keyboard.type(post_text, delay=10)
    except Exception as e:
        print(f"    WARNING: Could not write text: {e}")

    page.wait_for_timeout(1000)

    # Upload photo — the correct compose toolbar button is "Add media to post",
    # NOT "Add image" (which is a sidebar button). No input[type="file"] exists
    # until the button is clicked, so we use expect_file_chooser.
    print("  Uploading photo...")
    try:
        media_btn = page.locator('[aria-label="Add media to post"]').first
        with page.expect_file_chooser(timeout=5000) as fc_info:
            media_btn.click(timeout=3000)
        file_chooser = fc_info.value
        file_chooser.set_files(image_path)
        print(f"    Photo attached via file chooser")
    except Exception as e:
        # Fallback: check if a file input appeared in the DOM
        file_input = page.locator('input[type="file"]')
        if file_input.count() > 0:
            file_input.first.set_input_files(image_path)
            print(f"    Fallback: photo set on input directly")
        else:
            return {"success": False, "url_bsky": "", "error": f"Could not attach photo: {e}"}

    # Wait for image upload to finish — Bluesky shows "Uploading images..." in the
    # compose header while processing.  Wait for that text to disappear before posting.
    print("  Waiting for photo to upload...")
    try:
        uploading = page.locator('text="Uploading images..."')
        uploading.wait_for(state="hidden", timeout=30000)
        print("    Photo upload complete")
    except Exception:
        print("    Upload indicator not found or timed out, waiting extra...")
        page.wait_for_timeout(10000)

    # NSFW: only handle content warning dialog if it appears automatically.
    # Do NOT proactively open the Labels dialog — just post directly.
    if is_nsfw:
        try:
            warning_dialog = page.locator('text="Add a content warning"')
            if warning_dialog.is_visible(timeout=2000):
                print("  Content warning dialog detected, selecting Nudity...")
                page.locator('text="Nudity"').click(timeout=3000)
                page.locator('button:has-text("Done")').click(timeout=3000)
                page.wait_for_timeout(500)
                print("    Nudity label applied")
        except Exception:
            print("    Content warning dialog not found, posting without label")

    if no_submit:
        print("  --no-submit: skipping post")
        return {"success": True, "url_bsky": "NO_SUBMIT", "error": ""}

    # Click "Post" button — dismiss any blocking overlay first
    print("  Posting...")
    try:
        # If "Close active dialog" overlay exists, dismiss it
        overlay = page.locator('[aria-label="Close active dialog"]')
        if overlay.count() > 0 and overlay.first.is_visible():
            overlay.first.click(timeout=2000)
            page.wait_for_timeout(500)
        post_btn = page.locator('[aria-label="Publish post"]').first
        post_btn.click(timeout=5000)
    except Exception as e:
        return {"success": False, "url_bsky": "", "error": f"Could not find Post button: {e}"}

    page.wait_for_timeout(5000)
    print("  Bluesky post published")
    return {"success": True, "url_bsky": "UPLOADED", "error": ""}


# ── Facebook Upload ──────────────────────────────────────────────
def upload_to_fb(page, caption, image_path, location="", feeling="", tag_people="", no_submit=False):
    """
    Post a photo with caption to Facebook personal timeline via browser automation.
    Flow: home → open composer → attach photo → set location → set feeling → tag people → write caption → Next → Post.
    Returns {"success": bool, "url_fb": str, "error": str}
    """
    if not image_path or not Path(image_path).exists():
        return {"success": False, "url_fb": "", "error": "No image file available"}

    # Navigate to Facebook
    print("  Opening Facebook...")
    try:
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return {"success": False, "url_fb": "", "error": "Timeout loading Facebook"}
    page.wait_for_timeout(3000)

    # Check if logged in
    if "/login" in page.url:
        return {"success": False, "url_fb": "", "error": "Not logged into Facebook — run --login first"}

    # Open the Create Post composer
    print("  Opening composer...")
    try:
        # Click the "What's on your mind" trigger bar
        composer_trigger = page.locator(
            '[role="button"]:has-text("What\'s on your mind"), '
            '[aria-label*="Create a post"], '
            '[aria-label*="What\'s on your mind"]'
        ).first
        composer_trigger.click(timeout=5000)
        page.wait_for_timeout(2000)
    except Exception as e:
        return {"success": False, "url_fb": "", "error": f"Could not open composer: {e}"}

    # Attach photo FIRST — Facebook resets the text area when a photo is added
    print("  Uploading photo...")
    try:
        # Try direct file input first (Facebook often has hidden file inputs)
        file_input = page.locator('input[type="file"][accept*="image"]')
        if file_input.count() > 0:
            file_input.first.set_input_files(image_path)
            print(f"    Photo attached via file input")
        else:
            # Click the photo/video button to trigger file chooser
            with page.expect_file_chooser(timeout=5000) as fc_info:
                photo_btn = page.locator(
                    '[aria-label="Photo/video"], [aria-label="Photo/Video"], '
                    '[aria-label*="photo"], [aria-label*="Photo"]'
                ).first
                photo_btn.click(timeout=3000)
            file_chooser = fc_info.value
            file_chooser.set_files(image_path)
            print(f"    Photo attached via file chooser")
    except Exception as e:
        # Last fallback: try any file input that appeared
        file_input = page.locator('input[type="file"]')
        if file_input.count() > 0:
            file_input.first.set_input_files(image_path)
            print(f"    Fallback: photo set on input directly")
        else:
            return {"success": False, "url_fb": "", "error": f"Could not attach photo: {e}"}

    # Wait for photo to upload/process
    print("  Waiting for photo to process...")
    page.wait_for_timeout(15000)

    # Location — click the red pin icon ("Check in") in the composer's "Add to your post" bar.
    # IMPORTANT: Multiple [aria-label="Check in"] exist on page. We must find the one INSIDE
    # the composer dialog (near "Add to your post" label), NOT the standalone check-in button.
    if location:
        try:
            print(f"  Setting location: {location}")

            # Step 1: Pick the [aria-label="Check in"] button with the HIGHEST Y value.
            # Multiple Check-in buttons exist; the toolbar one is always at the bottom of the
            # composer (highest Y). The standalone check-in is higher up on the page.
            btn_pos = page.evaluate("""() => {
                const btns = document.querySelectorAll('[aria-label="Check in"]');
                if (btns.length === 0) return {ok: false, reason: 'no [aria-label="Check in"] found'};

                let best = null;
                let bestY = -1;
                for (const btn of btns) {
                    const r = btn.getBoundingClientRect();
                    const cy = r.top + r.height / 2;
                    if (r.width > 0 && r.height > 0 && cy > bestY) {
                        bestY = cy;
                        best = btn;
                    }
                }

                if (!best) return {ok: false, reason: 'no visible Check-in button'};

                const r = best.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;

                // Clear all overlays above the button
                const stack = document.elementsFromPoint(cx, cy);
                let cleared = 0;
                for (const el of stack) {
                    if (el === best || best.contains(el) || el.contains(best)) break;
                    el.style.pointerEvents = 'none';
                    cleared++;
                }
                return {ok: true, x: cx, y: cy, cleared: cleared,
                        btnY: Math.round(cy)};
            }""")
            if not btn_pos.get("ok"):
                raise Exception(btn_pos.get("reason"))
            print(f"    Clicked Check-in pin at y={btn_pos['btnY']}")
            page.mouse.click(btn_pos["x"], btn_pos["y"])

            # Wait for the location search panel to appear
            page.wait_for_timeout(3000)

            # Step 2: Type location in the search field
            page.keyboard.type(location, delay=50)
            print(f"    Typed: {location}")
            page.wait_for_timeout(3000)

            # Step 3: Click first matching result
            picked = page.evaluate("""(query) => {
                const norm = s => s.toLowerCase().replace(/-/g, ' ');
                const q = norm(query);
                // Find the "Results" heading to only match items below it
                let resultsTop = 0;
                const spans = document.querySelectorAll('span');
                for (const s of spans) {
                    if (s.textContent.trim() === 'Results') {
                        resultsTop = s.getBoundingClientRect().bottom;
                        break;
                    }
                }
                // Check <li>, <div role="option">, <div role="button"> as possible result containers
                const candidates = document.querySelectorAll('li, [role="option"], [role="listbox"] [role="button"]');
                const matches = [];
                for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0 || r.height < 20) continue;
                    if (resultsTop > 0 && r.top < resultsTop) continue;
                    const text = el.textContent.trim();
                    if (norm(text).includes(q)) {
                        matches.push({text: text.substring(0, 80), top: Math.round(r.top),
                                      x: r.left + r.width / 2, y: r.top + r.height / 2});
                    }
                }
                // If no text match, just grab the first result item below resultsTop
                if (matches.length === 0 && resultsTop > 0) {
                    for (const el of candidates) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 20 && r.top > resultsTop) {
                            matches.push({text: el.textContent.trim().substring(0, 80),
                                          top: Math.round(r.top),
                                          x: r.left + r.width / 2, y: r.top + r.height / 2});
                            break;
                        }
                    }
                }
                if (matches.length === 0) return {ok: false, reason: 'no matching results', resultsTop: resultsTop};
                return {ok: true, x: matches[0].x, y: matches[0].y,
                        text: matches[0].text,
                        all: matches.map(m => m.text)};
            }""", location)
            if picked.get("ok"):
                print(f"    Results: {picked['all']}")
                page.mouse.click(picked["x"], picked["y"])
                print(f"    Selected: {picked.get('text')}")
            else:
                print(f"    WARNING: {picked.get('reason')} (resultsTop={picked.get('resultsTop')})")
                try:
                    page.locator('[aria-label="Back"]').first.click(timeout=3000)
                except Exception:
                    pass
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"    WARNING: Could not set location: {e}")

    # Feeling — click the smiley icon ("Feeling/activity") in the "Add to your post" bar.
    # Same pattern as location: multiple buttons may exist; pick the one with highest Y.
    if feeling:
        try:
            print(f"  Setting feeling: {feeling}")

            btn_pos = page.evaluate(r"""() => {
                const labels = ['Feeling/activity', 'Feeling/Activity', 'Feeling / activity'];
                let allBtns = [];
                for (const label of labels) {
                    const found = document.querySelectorAll('[aria-label="' + label + '"]');
                    for (const b of found) allBtns.push(b);
                }
                if (allBtns.length === 0) return {ok: false, reason: 'no Feeling/activity button found'};

                let best = null;
                let bestY = -1;
                for (const btn of allBtns) {
                    const r = btn.getBoundingClientRect();
                    const cy = r.top + r.height / 2;
                    if (r.width > 0 && r.height > 0 && cy > bestY) {
                        bestY = cy;
                        best = btn;
                    }
                }
                if (!best) return {ok: false, reason: 'no visible Feeling/activity button'};

                const r = best.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;

                const stack = document.elementsFromPoint(cx, cy);
                let cleared = 0;
                for (const el of stack) {
                    if (el === best || best.contains(el) || el.contains(best)) break;
                    el.style.pointerEvents = 'none';
                    cleared++;
                }
                return {ok: true, x: cx, y: cy, cleared: cleared, btnY: Math.round(cy)};
            }""")

            if not btn_pos.get("ok"):
                raise Exception(btn_pos.get("reason"))
            print(f"    Clicked Feeling/activity at y={btn_pos['btnY']}")
            page.mouse.click(btn_pos["x"], btn_pos["y"])
            page.wait_for_timeout(3000)

            picked = page.evaluate(r"""(target) => {
                const q = target.toLowerCase().trim();
                const candidates = document.querySelectorAll(
                    '[role="button"], [role="option"], [role="listitem"], li, div[tabindex]');
                const matches = [];
                for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0 || r.height < 15) continue;
                    const text = el.textContent.trim().toLowerCase();
                    if (text === q || text.startsWith(q)) {
                        matches.push({
                            text: el.textContent.trim().substring(0, 40),
                            top: Math.round(r.top),
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2
                        });
                    }
                }
                if (matches.length === 0) return {ok: false, reason: 'no matching feeling: ' + target};
                matches.sort((a, b) => a.top - b.top);
                return {ok: true, x: matches[0].x, y: matches[0].y, text: matches[0].text};
            }""", feeling)

            if picked.get("ok"):
                page.mouse.click(picked["x"], picked["y"])
                print(f"    Selected feeling: {picked.get('text')}")
            else:
                print(f"    WARNING: {picked.get('reason')}")
                try:
                    page.locator('[aria-label="Back"]').first.click(timeout=3000)
                except Exception:
                    pass
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"    WARNING: Could not set feeling: {e}")

    # Tag people — click the "Tag people" icon in the "Add to your post" bar.
    # Same pattern as location: highest Y button + overlay clearing.
    if tag_people:
        handles = [h.strip() for h in tag_people.split(",") if h.strip()]
        if handles:
            try:
                print(f"  Tagging {len(handles)} people: {', '.join(handles)}")

                btn_pos = page.evaluate(r"""() => {
                    const labels = ['Tag people', 'Tag People', 'Tag friends'];
                    let allBtns = [];
                    for (const label of labels) {
                        const found = document.querySelectorAll('[aria-label="' + label + '"]');
                        for (const b of found) allBtns.push(b);
                    }
                    if (allBtns.length === 0) return {ok: false, reason: 'no Tag people button found'};

                    let best = null;
                    let bestY = -1;
                    for (const btn of allBtns) {
                        const r = btn.getBoundingClientRect();
                        const cy = r.top + r.height / 2;
                        if (r.width > 0 && r.height > 0 && cy > bestY) {
                            bestY = cy;
                            best = btn;
                        }
                    }
                    if (!best) return {ok: false, reason: 'no visible Tag people button'};

                    const r = best.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;

                    const stack = document.elementsFromPoint(cx, cy);
                    let cleared = 0;
                    for (const el of stack) {
                        if (el === best || best.contains(el) || el.contains(best)) break;
                        el.style.pointerEvents = 'none';
                        cleared++;
                    }
                    return {ok: true, x: cx, y: cy, cleared: cleared, btnY: Math.round(cy)};
                }""")

                if not btn_pos.get("ok"):
                    raise Exception(btn_pos.get("reason"))
                print(f"    Clicked Tag people at y={btn_pos['btnY']}")
                page.mouse.click(btn_pos["x"], btn_pos["y"])
                page.wait_for_timeout(3000)

                for i, handle in enumerate(handles):
                    print(f"    Searching for: {handle}")

                    # Step 1: Find and click the tag people search input.
                    # Must be in the CENTER of the page (left > 300) to avoid sidebar inputs.
                    # The tag people panel is a modal in the center, its search input is at ~left=425.
                    search_pos = page.evaluate("""() => {
                        const inputs = document.querySelectorAll('input');
                        let best = null;
                        let bestY = -1;
                        const found = [];
                        for (const inp of inputs) {
                            const r = inp.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            found.push({ph: inp.placeholder, type: inp.type, left: Math.round(r.left),
                                        top: Math.round(r.top), w: Math.round(r.width)});
                            // Must be in center area (tag people panel), not sidebar
                            if (r.left < 300) continue;
                            const ph = (inp.placeholder || '').trim().toLowerCase();
                            if (ph.includes('search')) {
                                const cy = r.top + r.height / 2;
                                if (cy > bestY) {
                                    bestY = cy;
                                    best = inp;
                                }
                            }
                        }
                        if (!best) return {ok: false, reason: 'no center Search input found', inputs: found};
                        const r = best.getBoundingClientRect();
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        // Clear overlays
                        const stack = document.elementsFromPoint(cx, cy);
                        for (const el of stack) {
                            if (el === best || best.contains(el) || el.contains(best)) break;
                            el.style.pointerEvents = 'none';
                        }
                        return {ok: true, x: cx, y: cy, left: Math.round(r.left),
                                top: Math.round(r.top), bottom: Math.round(r.bottom),
                                placeholder: best.placeholder, inputs: found};
                    }""")
                    if not search_pos.get("ok"):
                        print(f"    WARNING: {search_pos.get('reason')}")
                        print(f"    All inputs: {search_pos.get('inputs')}")
                        continue
                    print(f"    Search input: placeholder='{search_pos.get('placeholder')}' at left={search_pos['left']}, y={search_pos['top']}")
                    page.mouse.click(search_pos["x"], search_pos["y"])
                    page.wait_for_timeout(500)

                    # Step 2: Type the handle
                    page.keyboard.type(handle, delay=50)
                    print(f"    Typed: {handle}")
                    page.wait_for_timeout(3000)

                    # Step 3: Click first search result — scope to panel container
                    # by walking up the DOM from the search input. This avoids
                    # matching sidebar elements that overlap in screen coordinates.
                    picked = page.evaluate("""() => {
                        // Find the search input in center area (tag people panel)
                        const inputs = document.querySelectorAll('input');
                        let searchInput = null;
                        let bestY = -1;
                        for (const inp of inputs) {
                            const r = inp.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0 || r.left < 300) continue;
                            const ph = (inp.placeholder || '').trim().toLowerCase();
                            if (ph.includes('search')) {
                                const cy = r.top + r.height / 2;
                                if (cy > bestY) { bestY = cy; searchInput = inp; }
                            }
                        }
                        if (!searchInput) return {ok: false, reason: 'search input not found'};

                        // Walk up from search input to find the panel container
                        // (a reasonably-sized element, ~300-600px wide, >150px tall)
                        let panel = searchInput;
                        for (let i = 0; i < 20; i++) {
                            if (!panel.parentElement) break;
                            panel = panel.parentElement;
                            const r = panel.getBoundingClientRect();
                            if (r.width >= 280 && r.width <= 600 && r.height > 150) break;
                        }
                        const panelRect = panel.getBoundingClientRect();

                        // Find the SEARCH or SUGGESTIONS heading within the panel
                        // to anchor results BELOW it (skips the TAGGED chips section).
                        let resultsTop = 0;
                        const panelSpans = panel.querySelectorAll('span');
                        for (const s of panelSpans) {
                            const t = s.textContent.trim();
                            if (t === 'SEARCH' || t === 'SUGGESTIONS') {
                                const sr = s.getBoundingClientRect();
                                if (sr.width > 0 && sr.height > 0) {
                                    resultsTop = sr.bottom;
                                    break;
                                }
                            }
                        }
                        // Fallback to search input bottom if no heading found
                        if (!resultsTop) resultsTop = searchInput.getBoundingClientRect().bottom;

                        const skip = new Set(['SEARCH','Search','Done','Next','Back','Cancel',
                                              'Tag people','Tag People','SUGGESTIONS','TAGGED']);
                        const els = panel.querySelectorAll('*');
                        for (const el of els) {
                            const r = el.getBoundingClientRect();
                            if (r.top < resultsTop) continue;
                            if (r.top > resultsTop + 200) continue;
                            if (r.height < 30 || r.height > 80) continue;
                            if (r.width < 100) continue;
                            const text = el.textContent.trim();
                            if (text.length < 3 || text.length > 100) continue;
                            if (skip.has(text)) continue;
                            if (text.startsWith('SEARCH') || text.startsWith('SUGGESTIONS')
                                || text.startsWith('TAGGED')) continue;
                            const cx = r.left + r.width / 2;
                            const cy = r.top + r.height / 2;
                            // Clear overlays
                            const stack = document.elementsFromPoint(cx, cy);
                            for (const ov of stack) {
                                if (ov === el || el.contains(ov) || ov.contains(el)) break;
                                ov.style.pointerEvents = 'none';
                            }
                            return {ok: true, text: text.substring(0, 80),
                                    x: cx, y: cy, tag: el.tagName,
                                    panelW: Math.round(panelRect.width),
                                    panelH: Math.round(panelRect.height)};
                        }
                        return {ok: false, reason: 'no result found in panel',
                                panelW: Math.round(panelRect.width),
                                panelH: Math.round(panelRect.height),
                                panelTag: panel.tagName};
                    }""")

                    if picked.get("ok"):
                        page.mouse.click(picked["x"], picked["y"])
                        print(f"    Tagged: {picked.get('text')}")
                    else:
                        print(f"    WARNING: Could not find '{handle}': {picked.get('reason')}")

                    page.wait_for_timeout(1500)

                    # Clear search for next handle — select all and delete
                    if i < len(handles) - 1:
                        page.keyboard.press("Control+a")
                        page.keyboard.press("Backspace")
                        page.wait_for_timeout(500)

                # Click Done — find it within the tag people panel (scoped via search input)
                done = page.evaluate(r"""() => {
                    // Find the search input in center area to locate the panel
                    const inputs = document.querySelectorAll('input');
                    let searchInput = null;
                    let bestY = -1;
                    for (const inp of inputs) {
                        const r = inp.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0 || r.left < 300) continue;
                        const ph = (inp.placeholder || '').trim().toLowerCase();
                        if (ph.includes('search')) {
                            const cy = r.top + r.height / 2;
                            if (cy > bestY) { bestY = cy; searchInput = inp; }
                        }
                    }
                    if (!searchInput) return {ok: false, reason: 'no search input'};

                    // Walk up to find the panel container
                    let panel = searchInput;
                    for (let i = 0; i < 20; i++) {
                        if (!panel.parentElement) break;
                        panel = panel.parentElement;
                        const r = panel.getBoundingClientRect();
                        if (r.width >= 280 && r.width <= 600 && r.height > 150) break;
                    }

                    // Find "Done" span/div within the panel
                    const els = panel.querySelectorAll('span, div, a');
                    for (const el of els) {
                        if (el.textContent.trim() === 'Done' && el.children.length === 0) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                const cx = r.left + r.width / 2;
                                const cy = r.top + r.height / 2;
                                // Clear overlays
                                const stack = document.elementsFromPoint(cx, cy);
                                for (const ov of stack) {
                                    if (ov === el || el.contains(ov) || ov.contains(el)) break;
                                    ov.style.pointerEvents = 'none';
                                }
                                return {ok: true, x: cx, y: cy};
                            }
                        }
                    }
                    return {ok: false, reason: 'Done not found in panel'};
                }""")
                if done.get("ok"):
                    page.mouse.click(done["x"], done["y"])
                    print(f"    Closed tag panel")
                else:
                    print(f"    WARNING: Could not find Done button")
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"    WARNING: Could not tag people: {e}")

    # Write caption AFTER photo — Facebook resets text when a photo is attached
    # Facebook has multiple textboxes; the caption is the topmost one (smallest top value)
    print("  Writing caption...")
    try:
        # Mark the topmost visible textbox with a data attribute so we can target it
        marked = page.evaluate("""() => {
            const boxes = document.querySelectorAll('[contenteditable="true"][role="textbox"]');
            let topmost = null;
            let minTop = Infinity;
            for (const el of boxes) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top < minTop) {
                    minTop = r.top;
                    topmost = el;
                }
            }
            if (topmost) {
                topmost.setAttribute('data-fb-caption', 'true');
                return {found: true, top: minTop};
            }
            return {found: false};
        }""")
        if marked.get("found"):
            textbox = page.locator('[data-fb-caption="true"]')
            textbox.fill(caption, timeout=5000)
            print(f"    Caption: '{caption[:60]}'")
        else:
            print(f"    WARNING: No visible textbox found")
    except Exception as e:
        print(f"    WARNING: Could not write caption: {e}")

    page.wait_for_timeout(1000)

    if no_submit:
        print("  --no-submit: skipping post")
        return {"success": True, "url_fb": "NO_SUBMIT", "error": ""}

    # Submit — Facebook has overlay divs that intercept pointer events.
    # Use elementsFromPoint() to find and disable ALL overlays above the button.
    print("  Posting...")

    def fb_clear_and_click(aria_label):
        """Find an enabled button by aria-label, disable overlays above it, then click."""
        info = page.evaluate("""(label) => {
            const btns = document.querySelectorAll('[aria-label="' + label + '"]');
            if (btns.length === 0) return {found: false, reason: 'no elements'};
            // Find the first visible AND enabled button
            let target = null;
            for (const btn of btns) {
                const r = btn.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 &&
                    btn.getAttribute('aria-disabled') !== 'true') {
                    target = btn;
                    break;
                }
            }
            if (!target) return {found: false, reason: 'none enabled'};
            const rect = target.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            // Get the full element stack at the click point (top to bottom)
            const stack = document.elementsFromPoint(cx, cy);
            let cleared = 0;
            for (const el of stack) {
                if (el === target || target.contains(el) || el.contains(target)) break;
                el.style.pointerEvents = 'none';
                cleared++;
            }
            return {found: true, top: rect.top, cleared: cleared};
        }""", aria_label)
        if not info.get("found"):
            return False
        page.wait_for_timeout(300)
        # Click the enabled button (skip disabled ones)
        btns = page.locator(f'[aria-label="{aria_label}"]')
        for i in range(btns.count()):
            btn = btns.nth(i)
            if btn.is_visible() and btn.get_attribute("aria-disabled") != "true":
                btn.click(timeout=5000)
                return True
        return False

    try:
        # Try Post/Share first (standard post flow), then Next (multi-step flow)
        submitted = False
        for label in ["Post", "Share", "Share now", "Publish", "Next"]:
            try:
                if fb_clear_and_click(label):
                    print(f"    Clicked '{label}'")
                    submitted = True
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        if submitted:
            # If we clicked Next, check if there's a second step
            # (look for Post/Share button after Next)
            for label in ["Post", "Share", "Share now", "Publish"]:
                try:
                    if fb_clear_and_click(label):
                        print(f"    Clicked '{label}'")
                        break
                except Exception:
                    continue
        else:
            # Dump available buttons for debugging
            buttons = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('[role="button"]'))
                    .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                    .map(b => {
                        const label = b.getAttribute('aria-label') || b.textContent?.trim().substring(0, 30);
                        const disabled = b.getAttribute('aria-disabled') === 'true' ? ' (disabled)' : '';
                        return label ? label + disabled : null;
                    })
                    .filter(Boolean).slice(0, 20);
            }""")
            return {"success": False, "url_fb": "", "error": f"Could not submit. Buttons: {buttons}"}
    except Exception as e:
        return {"success": False, "url_fb": "", "error": f"Could not submit post: {e}"}

    page.wait_for_timeout(5000)
    print("  Facebook post published")
    return {"success": True, "url_fb": "UPLOADED", "error": ""}


# ── DA Upload ─────────────────────────────────────────────────
def upload_to_da(page, row, desc_full, tags, groups, no_submit=False):
    """
    Automate the DA Sta.sh submission form.
    Returns {"success": bool, "deviation_url": str, "error": str}
    """
    stash_url = row.get("stash_url_nsfw", "").strip()
    if not stash_url:
        return {"success": False, "deviation_url": "", "error": "No stash_url_nsfw"}

    # ── Navigate to Sta.sh ────────────────────────────────────
    print(f"  Navigating to {stash_url}")
    try:
        page.goto(stash_url, wait_until="networkidle", timeout=30000)
    except PlaywrightTimeout:
        page.goto(stash_url, wait_until="domcontentloaded", timeout=15000)

    page.wait_for_timeout(2000)

    # Check page loaded
    if "not found" in page.title().lower() or page.locator(".error-404").count() > 0:
        return {"success": False, "deviation_url": "",
                "error": "Sta.sh page not found — URL invalid or item already published"}

    # ── Click "Edit or Submit" (below the photo, NOT the top nav "+Submit") ──
    print("  Looking for Edit/Submit button...")
    try:
        submit_link = page.get_by_text("Edit or Submit", exact=True)
        submit_link.scroll_into_view_if_needed()
        submit_link.click()
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception as e:
        # Fallback: JS to find the exact button
        clicked = page.evaluate("""
            const els = Array.from(document.querySelectorAll('a, button'));
            const target = els.find(el => el.textContent.trim() === 'Edit or Submit');
            if (target) { target.scrollIntoView({block: 'center'}); target.click(); return true; }
            return false;
        """)
        if not clicked:
            return {"success": False, "deviation_url": "",
                    "error": f"Could not find 'Edit or Submit' button: {e}"}
        page.wait_for_timeout(3000)

    page.wait_for_timeout(2000)

    # ── Diagnose form structure ──────────────────────────────
    print("  Analyzing form structure...")
    form_info = page.evaluate("""
        () => {
            const info = {};

            // Find all contenteditable elements
            const editables = document.querySelectorAll('[contenteditable="true"]');
            info.contenteditables = Array.from(editables).map((el, i) => ({
                index: i,
                tag: el.tagName,
                className: el.className.substring(0, 80),
                placeholder: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
                text: el.textContent.substring(0, 50),
                parent: el.parentElement?.className?.substring(0, 80) || '',
            }));

            // Find all input elements
            const inputs = document.querySelectorAll('input, textarea');
            info.inputs = Array.from(inputs).slice(0, 20).map(el => ({
                tag: el.tagName,
                type: el.type,
                name: el.name || '',
                placeholder: el.placeholder || '',
                className: el.className.substring(0, 80),
                value: el.value?.substring(0, 30) || '',
            }));

            // Find tag-related elements
            info.tagElements = document.querySelectorAll('[class*="tag" i]').length;

            // Find text that looks like form labels
            const labels = Array.from(document.querySelectorAll('h2, h3, h4, label, [class*="label"]'))
                .map(el => el.textContent.trim()).filter(t => t.length > 0 && t.length < 50);
            info.labels = labels.slice(0, 20);

            // Find buttons
            const buttons = Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim())
                .filter(t => t.length > 0 && t.length < 50);
            info.buttons = buttons.slice(0, 20);

            return info;
        }
    """)

    print(f"  Contenteditables ({len(form_info.get('contenteditables', []))}):")
    for ce in form_info.get("contenteditables", []):
        print(f"    [{ce['index']}] <{ce['tag']}> placeholder='{ce['placeholder']}' class='{ce['className'][:50]}'")
    print(f"  Inputs ({len(form_info.get('inputs', []))}):")
    for inp in form_info.get("inputs", []):
        print(f"    <{inp['tag']}> type={inp['type']} name='{inp['name']}' placeholder='{inp['placeholder']}'")
    print(f"  Labels: {form_info.get('labels', [])}")
    print(f"  Buttons: {form_info.get('buttons', [])}")
    print(f"  Tag-related elements: {form_info.get('tagElements', 0)}")

    # ── Fill form fields ─────────────────────────────────────
    print("\n  Filling form...")
    title = row.get("title", "").strip()
    nsfw_flag = row.get("da_nsfw_flag", "FALSE").strip().upper()

    def js_escape(s):
        return s.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")

    # 1. TITLE — input[name="title"] (React controlled)
    print("  Setting title...")
    page.evaluate(f"""
        (() => {{
            const el = document.querySelector('input[name="title"]');
            if (!el) return 'not_found';
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, '{js_escape(title)}');
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return el.value;
        }})()
    """)

    # 2. MATURE CHECKBOX — input[name="matureContent"]
    if nsfw_flag == "TRUE":
        print("  Setting mature flag...")
        page.evaluate("""
            (() => {
                const cb = document.querySelector('input[name="matureContent"]');
                if (cb && !cb.checked) cb.click();
            })()
        """)

    # 3. TAGS — the unnamed text input (name="", placeholder="")
    #    This input has zero dimensions until focused, so we use JS to
    #    find/scroll/focus it (bypasses visibility), then Playwright keyboard.
    #    DA may auto-suggest/pre-populate tags — count existing before adding.
    existing_tag_count = page.evaluate("""
        (() => {
            // DA tag chips are SPAN elements with class "reset-button" and "ds-card"
            return document.querySelectorAll('span[class*="reset-button"][class*="ds-card"]').length;
        })()
    """)
    if existing_tag_count > 0:
        print(f"  DA has {existing_tag_count} pre-existing tags")
    print(f"  Adding tags (limit {TAG_LIMIT}, have {len(tags)})...")
    tag_activated = page.evaluate("""
        (() => {
            const inputs = Array.from(document.querySelectorAll('input'));
            const tagInput = inputs.find(el =>
                el.type === 'text' && el.name !== 'title' && el.placeholder !== 'Search'
            );
            if (!tagInput) return false;
            tagInput.scrollIntoView({block: 'center'});
            tagInput.focus();
            tagInput.click();
            return true;
        })()
    """)
    if tag_activated:
        page.wait_for_timeout(500)
        entered = 0
        for tag in tags:
            # Check chip count before each tag — stop at limit
            chip_count = page.evaluate(
                "(() => document.querySelectorAll('span[class*=\"reset-button\"][class*=\"ds-card\"]').length)()"
            )
            if chip_count >= TAG_LIMIT:
                print(f"    Reached {TAG_LIMIT} tag chips, stopping ({entered} tags entered)")
                break
            page.keyboard.type(tag, delay=50)
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            entered += 1
        final_count = page.evaluate(
            "(() => document.querySelectorAll('span[class*=\"reset-button\"][class*=\"ds-card\"]').length)()"
        )
        print(f"    Tags: {entered} entered, {final_count} chips total")
    else:
        print("    WARNING: Could not find tag input")

    # 4. DESCRIPTION — TipTap ProseMirror contenteditable
    print("  Setting description...")
    page.evaluate(f"""
        (() => {{
            const el = document.querySelector('.tiptap.ProseMirror, [contenteditable="true"]');
            if (!el) return 'not_found';
            el.focus();
            document.execCommand('selectAll');
            document.execCommand('insertText', false, `{js_escape(desc_full)}`);
            return 'set';
        }})()
    """)

    # ── Gallery selection (dropdown, not a modal) ───────────────
    galleries = [g.strip() for g in row.get("da_gallery", "Featured").split(",") if g.strip()]
    if galleries and not (len(galleries) == 1 and galleries[0] == "Featured"):
        print(f"  Setting galleries: {', '.join(galleries)}")
        try:
            # Click the Gallery dropdown (shows "Featured" with a chevron arrow)
            gal_opened = page.evaluate("""
                (() => {
                    const labels = Array.from(document.querySelectorAll('h2, h3, h4, label, [class*="label"]'));
                    const galLabel = labels.find(el => el.textContent.trim() === 'Gallery');
                    if (!galLabel) return 'label_not_found';
                    galLabel.scrollIntoView({block: 'center'});

                    // Walk up from the Gallery label to find the dropdown showing "Featured"
                    let container = galLabel.parentElement;
                    for (let i = 0; i < 4 && container; i++) {
                        for (const child of container.children) {
                            if (child !== galLabel && !child.contains(galLabel)
                                && child.textContent.trim().startsWith('Featured')) {
                                child.click();
                                return 'clicked_dropdown';
                            }
                        }
                        container = container.parentElement;
                    }
                    return 'dropdown_not_found';
                })()
            """)
            print(f"    Gallery trigger: {gal_opened}")
            page.wait_for_timeout(2000)

            # Gallery list opens inline (replaces form view, not a new modal)
            for gal_name in galleries:
                if gal_name == "Featured":
                    continue
                # TreeWalker on full page to find gallery name in the list
                clicked = page.evaluate("""
                    (galName) => {
                        const walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT
                        );
                        while (walker.nextNode()) {
                            if (walker.currentNode.textContent.trim() === galName) {
                                const el = walker.currentNode.parentElement;
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """, gal_name)
                if clicked:
                    page.wait_for_timeout(500)
                    print(f"    Selected gallery: {gal_name}")
                else:
                    print(f"    WARNING: Gallery '{gal_name}' not found")
        except Exception as e:
            print(f"  WARNING: Gallery selection failed: {e}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

    # ── Groups — checkbox list in a ReactModal, confirm with Save ───
    if groups:
        print(f"  Submitting to {len(groups)} group(s)...")
        try:
            # Count modals before opening group dialog
            modals_before = page.locator('.ReactModal__Content--after-open').count()

            # Click the "Add" button next to "Submit to Group"
            opened = page.evaluate("""
                (() => {
                    const labels = Array.from(document.querySelectorAll('h2, h3, h4, label, [class*="label"]'));
                    const stgLabel = labels.find(el => el.textContent.trim() === 'Submit to Group');
                    if (!stgLabel) return 'label_not_found';

                    const stgRect = stgLabel.getBoundingClientRect();
                    const addBtns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.textContent.trim() === 'Add');

                    let closest = null, closestDist = Infinity;
                    for (const btn of addBtns) {
                        const dist = Math.abs(btn.getBoundingClientRect().top - stgRect.top);
                        if (dist < closestDist) { closestDist = dist; closest = btn; }
                    }
                    if (closest) {
                        closest.scrollIntoView({block: 'center'});
                        closest.click();
                        return 'clicked_add (dist: ' + Math.round(closestDist) + 'px)';
                    }
                    stgLabel.click();
                    return 'clicked_label';
                })()
            """)
            print(f"    Dialog trigger: {opened}")
            page.wait_for_timeout(2000)

            # Check if a NEW modal appeared
            modals_after = page.locator('.ReactModal__Content--after-open').count()
            if modals_after <= modals_before:
                print(f"    WARNING: No group dialog opened")
            else:
                dialog = page.locator('.ReactModal__Content--after-open').nth(modals_before)

                # The dialog is a checkbox list of groups — check each one
                for gp in groups:
                    print(f"    Group: {gp['group']}")
                    checked = dialog.evaluate("""
                        (el, groupName) => {
                            const lower = groupName.toLowerCase();
                            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                            while (walker.nextNode()) {
                                if (walker.currentNode.textContent.trim().toLowerCase() === lower) {
                                    walker.currentNode.parentElement.click();
                                    return 'checked';
                                }
                            }
                            return 'not_found';
                        }
                    """, gp["group"])
                    print(f"      {checked}")
                    page.wait_for_timeout(1000)

                    # After checking a group, a folder dropdown appears (shows "Featured")
                    # Select the desired folder via <select> (React-compatible) or custom dropdown
                    if checked == 'checked' and gp.get("folder"):
                        folder_result = dialog.evaluate("""
                            (el, folderName) => {
                                const selects = el.querySelectorAll('select');
                                if (selects.length > 0) {
                                    const sel = selects[selects.length - 1];
                                    const opts = Array.from(sel.options);
                                    const match = opts.find(o =>
                                        o.text.trim().toLowerCase() === folderName.toLowerCase()
                                    );
                                    if (match) {
                                        // Use React-compatible native setter
                                        const nativeSetter = Object.getOwnPropertyDescriptor(
                                            HTMLSelectElement.prototype, 'value'
                                        ).set;
                                        nativeSetter.call(sel, match.value);
                                        sel.dispatchEvent(new Event('input', {bubbles: true}));
                                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                                        return 'selected_via_select: ' + match.text;
                                    }
                                    return 'folder_not_in_select: ' + opts.map(o => o.text).join(', ');
                                }
                                // No <select> found — look for custom dropdown showing "Featured"
                                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                                while (walker.nextNode()) {
                                    if (walker.currentNode.textContent.trim() === 'Featured') {
                                        const dropEl = walker.currentNode.parentElement;
                                        dropEl.click();
                                        return 'clicked_featured_dropdown';
                                    }
                                }
                                return 'no_folder_dropdown';
                            }
                        """, gp["folder"])
                        print(f"      Folder: {folder_result}")
                        page.wait_for_timeout(500)

                        # If we clicked a custom dropdown, wait and select the folder
                        if folder_result == 'clicked_featured_dropdown':
                            page.wait_for_timeout(1000)
                            folder_picked = dialog.evaluate("""
                                (el, folderName) => {
                                    const lower = folderName.toLowerCase();
                                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                                    while (walker.nextNode()) {
                                        if (walker.currentNode.textContent.trim().toLowerCase() === lower) {
                                            walker.currentNode.parentElement.click();
                                            return 'selected';
                                        }
                                    }
                                    return 'not_found';
                                }
                            """, gp["folder"])
                            print(f"      Folder pick: {folder_picked}")
                            page.wait_for_timeout(500)

                # Click Save to confirm — use JS inside the dialog modal
                page.wait_for_timeout(500)
                save_result = dialog.evaluate("""
                    (el) => {
                        const btns = el.querySelectorAll('button');
                        for (const btn of btns) {
                            if (btn.textContent.trim() === 'Save') {
                                btn.scrollIntoView({block: 'center'});
                                btn.click();
                                return 'clicked_save';
                            }
                        }
                        // List all buttons for debug
                        return 'save_not_found: ' + Array.from(btns).map(b => b.textContent.trim()).join(', ');
                    }
                """)
                print(f"    Save: {save_result}")
                page.wait_for_timeout(2000)

        except Exception as e:
            print(f"    Groups FAILED: {e}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

    # Dismiss any accidental "Discard deviation?" dialog
    try:
        discard_cancel = page.locator("button:has-text('Cancel')").first
        if discard_cancel.is_visible(timeout=500):
            discard_cancel.click()
            page.wait_for_timeout(500)
    except Exception:
        pass

    # ── Premium Download: ensure OFF ──────────────────────────
    print("  Checking Premium Download toggle...")
    try:
        pd_result = page.evaluate("""
            (() => {
                // DA toggle: <label for="id">text</label><button id="id" aria-pressed="true/false">
                const labels = document.querySelectorAll('label');
                for (const label of labels) {
                    if (label.textContent.toLowerCase().includes('premium download')) {
                        const btnId = label.getAttribute('for');
                        const btn = btnId ? document.getElementById(btnId)
                            : label.nextElementSibling;
                        if (!btn) return 'toggle_btn_not_found';
                        const isOn = btn.getAttribute('aria-pressed') === 'true';
                        if (isOn) {
                            btn.click();
                            return 'disabled';
                        }
                        return 'already_off';
                    }
                }
                return 'label_not_found';
            })()
        """)
        print(f"    Premium Download: {pd_result}")
    except Exception as e:
        print(f"    WARNING: Premium Download toggle: {e}")

    # ── Advanced settings ─────────────────────────────────────
    print("  Checking Advanced settings...")
    try:
        adv_btn = page.get_by_text("Advanced settings", exact=True).first
        adv_btn.scroll_into_view_if_needed()
        adv_btn.click()
        page.wait_for_timeout(1000)
        # Turn OFF the "Allow free download of source file" toggle
        dl_result = page.evaluate("""
            (() => {
                // DA toggle: <label for="id">text</label><button id="id" aria-pressed="true/false">
                // Find the label, then its linked button
                const labels = document.querySelectorAll('label');
                for (const label of labels) {
                    if (label.textContent.toLowerCase().includes('free download')) {
                        // Toggle is a sibling <button> with aria-pressed
                        const btnId = label.getAttribute('for');
                        const btn = btnId ? document.getElementById(btnId)
                            : label.nextElementSibling;
                        if (!btn) return 'toggle_btn_not_found';
                        const isOn = btn.getAttribute('aria-pressed') === 'true';
                        if (isOn) {
                            btn.click();
                            return 'disabled';
                        }
                        return 'already_off';
                    }
                }
                // Fallback: TreeWalker to find text, then look for closest label ancestor
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    if (walker.currentNode.textContent.toLowerCase().includes('free download of source')) {
                        let el = walker.currentNode.parentElement;
                        for (let i = 0; i < 6; i++) {
                            if (!el) break;
                            if (el.tagName === 'LABEL') {
                                el.click();
                                return 'toggled_via_ancestor_label';
                            }
                            el = el.parentElement;
                        }
                        return 'no_label_ancestor';
                    }
                }
                return 'label_not_found';
            })()
        """)
        print(f"    Free download: {dl_result}")
    except Exception as e:
        print(f"    WARNING: Advanced settings: {e}")

    # ── Validate before submit ────────────────────────────────
    validation = page.evaluate("""
        (() => {
            const titleEl = document.querySelector('input[name="title"]');
            const title = titleEl ? titleEl.value.trim() : '';
            // DA tag chips are SPAN elements with class "reset-button" and "ds-card"
            const tagCount = document.querySelectorAll('span[class*="reset-button"][class*="ds-card"]').length;
            return { tagCount, title, valid: title.length > 0 };
        })()
    """)

    print(f"  Validation: title='{validation['title'][:40]}', tags~{validation['tagCount']}, valid={validation['valid']}")

    if not validation["title"]:
        return {"success": False, "deviation_url": "", "error": "NO_TITLE — title field is empty"}

    # ── Submit ────────────────────────────────────────────────
    if no_submit:
        print("  --no-submit flag set, skipping submission")
        return {"success": True, "deviation_url": "NO_SUBMIT", "error": ""}

    print("  Submitting...")
    # Use JS to click the Submit button INSIDE the form modal (not the nav bar one)
    submit_result = page.evaluate("""
        (() => {
            // Find Submit button inside a ReactModal (the form), not the nav bar
            const modals = document.querySelectorAll('.ReactModal__Content--after-open');
            for (const modal of modals) {
                const btns = modal.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.textContent.trim() === 'Submit' && btn.getAttribute('role') !== 'menuitem') {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return 'clicked';
                    }
                }
            }
            return 'not_found';
        })()
    """)

    if submit_result == "not_found":
        return {"success": False, "deviation_url": "", "error": "Submit button not found inside form modal"}

    # ── Wait for redirect to deviation page ───────────────────
    print("  Waiting for redirect to deviation page...")
    try:
        page.wait_for_url("**/art/**", timeout=30000)
    except PlaywrightTimeout:
        # Check for "Boost" dialog
        try:
            boost = page.locator("button:has-text('Maybe Later'), a:has-text('Maybe Later')").first
            if boost.is_visible():
                boost.click()
                page.wait_for_url("**/art/**", timeout=15000)
        except Exception:
            pass

    deviation_url = page.url
    if "/art/" in deviation_url:
        print(f"  Published: {deviation_url}")
        return {"success": True, "deviation_url": deviation_url, "error": ""}
    else:
        return {"success": False, "deviation_url": "",
                "error": f"Redirect failed — ended up at {deviation_url}"}


# ── Main ──────────────────────────────────────────────────────
def main():
    args = parse_args()

    # Login mode: open browser for platform logins, then exit
    if args.login:
        print(f"Opening browser for login (profile: {args.profile})")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(args.profile),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            # Hide automation flags so sites like X.com don't block login
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page.goto("https://500px.com/login", wait_until="domcontentloaded", timeout=30000)
            print("Log into 500px in the browser window.")
            print("Press ENTER when done (will open 35photo next)...")
            input()
            page.goto("https://35photo.pro/login/", wait_until="domcontentloaded", timeout=30000)
            print("Log into 35photo in the browser window.")
            print("Press ENTER when done (will open VK next)...")
            input()
            page.goto("https://vk.com/login", wait_until="domcontentloaded", timeout=30000)
            print("Log into VK in the browser window.")
            print("Press ENTER when done (will open X next)...")
            input()
            page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=30000)
            print("Log into X.com in the browser window.")
            print("Press ENTER when done (will open Bluesky next)...")
            input()
            page.goto("https://bsky.app/", wait_until="domcontentloaded", timeout=30000)
            print("Log into Bluesky in the browser window.")
            print("Press ENTER when done (will open Facebook next)...")
            input()
            page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=30000)
            print("Log into Facebook in the browser window.")
            print("Press ENTER when done (will open DeviantArt next)...")
            input()
            page.goto("https://www.deviantart.com/users/login", wait_until="domcontentloaded", timeout=30000)
            print("Log into DeviantArt in the browser window.")
            print("Press ENTER when done...")
            input()
            ctx.close()
        print("Logins saved. You can now run: python upload.py --no-submit")
        sys.exit(0)

    # Load config
    config = load_config(args.config)

    # Load and filter CSV
    all_rows = load_queue(args.csv)
    target_rows = filter_rows(all_rows, args.row)

    if not target_rows:
        print("No rows to upload.")
        print("Check that: status=Approved, platforms contains a supported platform, and no AUTO fields remain.")
        sys.exit(0)

    print(f"Found {len(target_rows)} row(s) to upload.")

    # Dry run
    if args.dry_run:
        print_dry_run(target_rows, config)
        sys.exit(0)

    # Check profile exists
    if not args.profile.exists():
        print("ERROR: No browser profile found. Run first:  python upload.py --login")
        sys.exit(1)

    # Launch browser
    print(f"\nLaunching browser (profile: {args.profile})")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(args.profile),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 900},
            slow_mo=100,
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        results_summary = []

        try:
            for i, row in enumerate(target_rows):
                print(f"\n{'=' * 60}")
                print(f"[{i + 1}/{len(target_rows)}] {row['upload_id']} — {row.get('title', '?')}")
                print(f"{'=' * 60}")

                platforms = get_row_platforms(row)
                print(f"  Platforms: {', '.join(sorted(platforms))}")

                desc_full = build_description(row, config)
                tags = prepare_tags(row.get("keywords", ""))
                errors = []
                image_path = None
                ok_500px = False
                ok_35p = False
                ok_vk = False
                ok_x = False
                ok_bsky = False
                ok_fb = False
                ok_da = False

                # ── 500PX (must run before DA) ────────────────────
                if "500PX" in platforms:
                    already = row.get("url_500px", "").strip()
                    if already:
                        print(f"\n  500px: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── 500px Upload ──")
                        # Download image from Sta.sh if not already downloaded
                        if not image_path:
                            stash_url = row.get("stash_url_nsfw", "").strip()
                            if stash_url:
                                image_path = download_stash_image(page, stash_url, row["upload_id"])
                            else:
                                print("  WARNING: No stash_url_nsfw — cannot download image")

                        tags_500px = tags[:TAG_LIMIT_500PX]
                        try:
                            result_500px = upload_to_500px(
                                page, row, desc_full, tags_500px, image_path, args.no_submit
                            )
                        except PlaywrightTimeout as e:
                            result_500px = {"success": False, "url_500px": "", "error": f"Timeout: {e}"}
                        except Exception as e:
                            result_500px = {"success": False, "url_500px": "", "error": f"Unexpected: {e}"}

                        if result_500px["success"]:
                            ok_500px = True
                            print(f"  500px: SUCCESS — {result_500px.get('url_500px', '')}")
                        else:
                            err = result_500px.get("error", "unknown")
                            print(f"  500px: FAILED — {err}")
                            errors.append(f"500px: {err}")
                            # Screenshot on failure
                            ts = datetime.now().strftime("%H%M%S")
                            shot_path = SCRIPT_DIR / f"error_{row['upload_id']}_500px_{ts}.png"
                            try:
                                page.screenshot(path=str(shot_path))
                                print(f"  Error screenshot: {shot_path}")
                            except Exception:
                                pass

                        # Update CSV with 500px result
                        url_500px = result_500px.get("url_500px", "")
                        if url_500px and url_500px not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_500px": url_500px})

                # ── 35photo (between 500px and DA) ────────────────
                if "35P" in platforms:
                    already = row.get("url_35p", "").strip()
                    if already:
                        print(f"\n  35photo: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── 35photo Upload ──")
                        # Download image from Sta.sh if not already downloaded
                        if not image_path:
                            stash_url = row.get("stash_url_nsfw", "").strip()
                            if stash_url:
                                image_path = download_stash_image(page, stash_url, row["upload_id"])
                            else:
                                print("  WARNING: No stash_url_nsfw — cannot download image")

                        tags_35p = tags[:TAG_LIMIT_500PX]
                        try:
                            result_35p = upload_to_35photo(
                                page, row, desc_full, tags_35p, image_path, args.no_submit
                            )
                        except PlaywrightTimeout as e:
                            result_35p = {"success": False, "url_35p": "", "error": f"Timeout: {e}"}
                        except Exception as e:
                            result_35p = {"success": False, "url_35p": "", "error": f"Unexpected: {e}"}

                        if result_35p["success"]:
                            ok_35p = True
                            print(f"  35photo: SUCCESS — {result_35p.get('url_35p', '')}")
                        else:
                            err = result_35p.get("error", "unknown")
                            print(f"  35photo: FAILED — {err}")
                            errors.append(f"35photo: {err}")
                            # Screenshot on failure
                            ts = datetime.now().strftime("%H%M%S")
                            shot_path = SCRIPT_DIR / f"error_{row['upload_id']}_35p_{ts}.png"
                            try:
                                page.screenshot(path=str(shot_path))
                                print(f"  Error screenshot: {shot_path}")
                            except Exception:
                                pass

                        # Update CSV with 35photo result
                        url_35p = result_35p.get("url_35p", "")
                        if url_35p and url_35p not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_35p": url_35p})

                # ── VK (API-based, between 35P and DA) ───────────
                if "VK" in platforms:
                    already = row.get("url_vk", "").strip()
                    if already:
                        print(f"\n  VK: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── VK Upload ──")
                        # Download image from Sta.sh if not already downloaded
                        if not image_path:
                            stash_url = row.get("stash_url_nsfw", "").strip()
                            if stash_url:
                                image_path = download_stash_image(page, stash_url, row["upload_id"])
                            else:
                                print("  WARNING: No stash_url_nsfw — cannot download image")

                        vk_tag_people = row.get("vk_tag_people", "").strip()
                        vk_groups = row.get("vk_groups", "").strip()
                        vk_group_caption = row.get("vk_group_caption", "").strip()

                        try:
                            result_vk = upload_to_vk(
                                page, desc_full, image_path,
                                vk_tag_people=vk_tag_people,
                                vk_groups=vk_groups,
                                vk_group_caption=vk_group_caption,
                                no_submit=args.no_submit,
                            )
                        except Exception as e:
                            result_vk = {"success": False, "url_vk": "", "error": f"Unexpected: {e}"}

                        if result_vk["success"]:
                            ok_vk = True
                            print(f"  VK: SUCCESS — {result_vk.get('url_vk', '')}")
                        else:
                            err = result_vk.get("error", "unknown")
                            print(f"  VK: FAILED — {err}")
                            errors.append(f"VK: {err}")

                        # Update CSV with VK result
                        url_vk = result_vk.get("url_vk", "")
                        vk_updates = {}
                        if url_vk and url_vk not in ("NO_SUBMIT",):
                            vk_updates["url_vk"] = url_vk
                        vk_gr = result_vk.get("vk_groups_result", "")
                        if vk_gr:
                            vk_updates["vk_groups_result"] = vk_gr
                        if vk_updates:
                            save_row_update(args.csv, row["upload_id"], vk_updates)

                # ── X (between VK and DA) ────────────────────────
                if "X" in platforms:
                    already = row.get("url_x", "").strip()
                    if already:
                        print(f"\n  X: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── X.com Upload ──")
                        # Download image from Sta.sh if not already downloaded
                        if not image_path:
                            stash_url = row.get("stash_url_nsfw", "").strip()
                            if stash_url:
                                image_path = download_stash_image(page, stash_url, row["upload_id"])
                            else:
                                print("  WARNING: No stash_url_nsfw — cannot download image")

                        post_text = build_x_post_text(row.get("title", ""), row.get("keywords", ""))
                        print(f"    Post text ({len(post_text)} chars): {post_text}")

                        try:
                            result_x = upload_to_x(page, post_text, image_path, args.no_submit)
                        except Exception as e:
                            result_x = {"success": False, "url_x": "", "error": f"Unexpected: {e}"}

                        if result_x["success"]:
                            ok_x = True
                            print(f"  X: SUCCESS — {result_x.get('url_x', '')}")
                        else:
                            err = result_x.get("error", "unknown")
                            print(f"  X: FAILED — {err}")
                            errors.append(f"X: {err}")

                        # Update CSV with X result
                        url_x = result_x.get("url_x", "")
                        if url_x and url_x not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_x": url_x})

                # ── BSKY (between X and FB) ─────────────────────
                if "BSKY" in platforms:
                    already = row.get("url_bsky", "").strip()
                    if already:
                        print(f"\n  BSKY: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── Bluesky Upload ──")
                        # Download image from Sta.sh if not already downloaded
                        if not image_path:
                            stash_url = row.get("stash_url_nsfw", "").strip()
                            if stash_url:
                                image_path = download_stash_image(page, stash_url, row["upload_id"])
                            else:
                                print("  WARNING: No stash_url_nsfw — cannot download image")

                        is_nsfw = row.get("da_nsfw_flag", "").strip().upper() == "TRUE"
                        post_text = build_bsky_post_text(row.get("title", ""), row.get("keywords", ""))
                        print(f"    Post text ({len(post_text)} chars): {post_text}")

                        try:
                            result_bsky = upload_to_bsky(page, post_text, image_path, is_nsfw, args.no_submit)
                        except Exception as e:
                            result_bsky = {"success": False, "url_bsky": "", "error": f"Unexpected: {e}"}

                        if result_bsky["success"]:
                            ok_bsky = True
                            print(f"  BSKY: SUCCESS — {result_bsky.get('url_bsky', '')}")
                        else:
                            err = result_bsky.get("error", "unknown")
                            print(f"  BSKY: FAILED — {err}")
                            errors.append(f"BSKY: {err}")

                        # Update CSV with BSKY result
                        url_bsky = result_bsky.get("url_bsky", "")
                        if url_bsky and url_bsky not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_bsky": url_bsky})

                # ── FB (between BSKY and DA) ───────────────────────
                if "FB" in platforms:
                    already = row.get("url_fb", "").strip()
                    if already:
                        print(f"\n  FB: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── Facebook Upload ──")
                        location_fb = row.get("location_500px", "").strip()
                        feeling_fb = row.get("fb_feeling", "").strip()
                        tag_people_fb = row.get("fb_tag_people", "").strip()
                        is_nsfw = row.get("da_nsfw_flag", "").strip().upper() == "TRUE"

                        # NSFW safety: never upload NSFW image to Facebook
                        # If NSFW, require stash_url_safe; if missing, fail hard
                        if is_nsfw:
                            safe_url = row.get("stash_url_safe", "").strip()
                            if not safe_url:
                                result_fb = {"success": False, "url_fb": "",
                                             "error": "NSFW photo has no stash_url_safe — refusing to upload to Facebook"}
                            else:
                                # Download the safe version specifically for FB
                                fb_image_path = download_stash_image(page, safe_url, row["upload_id"] + "_safe")
                                fb_caption = build_description_fb(row)
                                print(f"    Caption: {fb_caption[:80]}...")
                                print(f"    Using safe image (NSFW flag set)")
                                try:
                                    result_fb = upload_to_fb(page, fb_caption, fb_image_path, location_fb, feeling_fb, tag_people_fb, args.no_submit)
                                except Exception as e:
                                    result_fb = {"success": False, "url_fb": "", "error": f"Unexpected: {e}"}
                                finally:
                                    # Clean up the separate safe image
                                    if fb_image_path and Path(fb_image_path).exists():
                                        try:
                                            Path(fb_image_path).unlink()
                                        except Exception:
                                            pass
                        else:
                            # Non-NSFW: use the regular image (already downloaded or download now)
                            if not image_path:
                                stash_url = row.get("stash_url_nsfw", "").strip()
                                if stash_url:
                                    image_path = download_stash_image(page, stash_url, row["upload_id"])
                                else:
                                    print("  WARNING: No Sta.sh URL — cannot download image")

                            fb_caption = build_description_fb(row)
                            print(f"    Caption: {fb_caption[:80]}...")

                            try:
                                result_fb = upload_to_fb(page, fb_caption, image_path, location_fb, feeling_fb, tag_people_fb, args.no_submit)
                            except Exception as e:
                                result_fb = {"success": False, "url_fb": "", "error": f"Unexpected: {e}"}

                        if result_fb["success"]:
                            ok_fb = True
                            print(f"  FB: SUCCESS — {result_fb.get('url_fb', '')}")
                        else:
                            err = result_fb.get("error", "unknown")
                            print(f"  FB: FAILED — {err}")
                            errors.append(f"FB: {err}")

                        # Update CSV with FB result
                        url_fb = result_fb.get("url_fb", "")
                        if url_fb and url_fb not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_fb": url_fb})

                # ── DA (must run last — consumes Sta.sh) ──────────
                if "DA" in platforms:
                    already = row.get("da_deviation_url", "").strip()
                    if already:
                        print(f"\n  DA: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── DeviantArt Upload ──")
                        groups = parse_groups(row.get("da_groups", ""))
                        try:
                            result_da = upload_to_da(page, row, desc_full, tags, groups, args.no_submit)
                        except PlaywrightTimeout as e:
                            result_da = {"success": False, "deviation_url": "", "error": f"Timeout: {e}"}
                        except Exception as e:
                            result_da = {"success": False, "deviation_url": "", "error": f"Unexpected: {e}"}

                        if result_da["success"]:
                            ok_da = True
                            print(f"  DA: SUCCESS — {result_da.get('deviation_url', '')}")
                        else:
                            err = result_da.get("error", "unknown")
                            print(f"  DA: FAILED — {err}")
                            errors.append(f"DA: {err}")
                            # Screenshot on failure
                            ts = datetime.now().strftime("%H%M%S")
                            shot_path = SCRIPT_DIR / f"error_{row['upload_id']}_da_{ts}.png"
                            try:
                                page.screenshot(path=str(shot_path))
                                print(f"  Error screenshot: {shot_path}")
                            except Exception:
                                pass

                        # Update CSV with DA result
                        da_url = result_da.get("deviation_url", "")
                        if da_url and da_url not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"da_deviation_url": da_url})

                # ── Clean up temp image ───────────────────────────
                if image_path and Path(image_path).exists():
                    try:
                        Path(image_path).unlink()
                        print(f"  Cleaned up temp file: {image_path}")
                    except Exception as e:
                        print(f"  WARNING: Could not delete temp file: {e}")

                # ── Update row status ─────────────────────────────
                # Check which platforms are now done (either this run or previously)
                # Re-read row to get latest URLs
                if not args.no_submit:
                    fresh_rows = load_queue(args.csv)
                    fresh_row = next((r for r in fresh_rows if r["upload_id"] == row["upload_id"]), row)

                    done_platforms = set()
                    if "500PX" in platforms and (ok_500px or fresh_row.get("url_500px", "").strip()):
                        done_platforms.add("500PX")
                    if "35P" in platforms and (ok_35p or fresh_row.get("url_35p", "").strip()):
                        done_platforms.add("35P")
                    if "VK" in platforms and (ok_vk or fresh_row.get("url_vk", "").strip()):
                        done_platforms.add("VK")
                    if "X" in platforms and (ok_x or fresh_row.get("url_x", "").strip()):
                        done_platforms.add("X")
                    if "BSKY" in platforms and (ok_bsky or fresh_row.get("url_bsky", "").strip()):
                        done_platforms.add("BSKY")
                    if "FB" in platforms and (ok_fb or fresh_row.get("url_fb", "").strip()):
                        done_platforms.add("FB")
                    if "DA" in platforms and (ok_da or fresh_row.get("da_deviation_url", "").strip()):
                        done_platforms.add("DA")

                    if done_platforms == platforms:
                        status = "Uploaded"
                    elif done_platforms:
                        status = "Partial"
                    else:
                        status = "Failed"

                    updates = {
                        "status": status,
                        "upload_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if errors:
                        # Strip newlines from Playwright error logs to prevent CSV corruption
                        clean_errors = "; ".join(e.replace("\n", " ").replace("\r", "") for e in errors)
                        updates["error_log"] = clean_errors[:200]
                    save_row_update(args.csv, row["upload_id"], updates)

                # Summary line
                summary_detail = []
                if "500PX" in platforms:
                    s = "done" if ok_500px or row.get("url_500px", "").strip() else "failed"
                    summary_detail.append(f"500px:{s}")
                if "35P" in platforms:
                    s = "done" if ok_35p or row.get("url_35p", "").strip() else "failed"
                    summary_detail.append(f"35photo:{s}")
                if "VK" in platforms:
                    s = "done" if ok_vk or row.get("url_vk", "").strip() else "failed"
                    summary_detail.append(f"VK:{s}")
                if "X" in platforms:
                    s = "done" if ok_x or row.get("url_x", "").strip() else "failed"
                    summary_detail.append(f"X:{s}")
                if "BSKY" in platforms:
                    s = "done" if ok_bsky or row.get("url_bsky", "").strip() else "failed"
                    summary_detail.append(f"BSKY:{s}")
                if "FB" in platforms:
                    s = "done" if ok_fb or row.get("url_fb", "").strip() else "failed"
                    summary_detail.append(f"FB:{s}")
                if "DA" in platforms:
                    s = "done" if ok_da or row.get("da_deviation_url", "").strip() else "failed"
                    summary_detail.append(f"DA:{s}")
                results_summary.append((row["upload_id"], ", ".join(summary_detail)))

        finally:
            # Clean up temp directory if empty
            if TEMP_DIR.exists():
                try:
                    TEMP_DIR.rmdir()  # only removes if empty
                except OSError:
                    pass
            context.close()

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for uid, detail in results_summary:
        print(f"  {uid} — {detail}")
    print(f"\n{len(results_summary)} row(s) processed.")


if __name__ == "__main__":
    main()
