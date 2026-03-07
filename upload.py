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
SUPPORTED_PLATFORMS = {"DA", "500PX", "35P", "VK", "X", "FB"}
TEMP_DIR = SCRIPT_DIR / "temp"

COLS = [
    "upload_id", "scheduled_date", "scheduled_time",
    "stash_url_nsfw", "stash_url_safe", "title", "caption", "keywords",
    "da_nsfw_flag", "category_500px", "category_35p", "caption_fb",
    "platforms", "status", "upload_timestamp",
    "da_deviation_url", "url_500px", "url_35p", "url_vk", "url_x", "url_fb",
    "notes", "error_log", "model_name", "da_gallery", "da_groups",
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
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


# ── Row filtering ─────────────────────────────────────────────
def get_row_platforms(row):
    """Return set of supported platforms for this row."""
    raw = [p.strip().upper() for p in row.get("platforms", "").split(",")]
    return SUPPORTED_PLATFORMS & set(raw)


def filter_rows(rows, target_id=None):
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
    """Assemble desc_full: caption + model respect + social links."""
    parts = [row.get("caption", "").strip()]

    # Model respect
    model = row.get("model_name", "").strip()
    if model:
        parts.append(f"\n\nModel: {model}. Please respect the model.")

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
    page.wait_for_timeout(10000)

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

    # Category
    if category:
        page.click('#category-input')
        page.wait_for_timeout(500)
        try:
            cat_option = page.locator(f'text="{category}"').first
            cat_option.scroll_into_view_if_needed()
            cat_option.click(timeout=3000)
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
    page.wait_for_timeout(15000)

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

    # Adult content checkbox
    if nsfw:
        try:
            adult_cb = page.locator('text="Adult content 18"').first
            adult_cb.click(timeout=3000)
        except Exception as e:
            print(f"    WARNING: Could not check adult content: {e}")

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
def upload_to_vk(page, desc_full, image_path, no_submit=False):
    """
    Post a photo to the VK wall via browser automation.
    Flow: feed → Create post → write caption → upload photo → Next → Publish.
    Returns {"success": bool, "url_vk": str, "error": str}
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
    page.wait_for_timeout(8000)

    page.wait_for_timeout(1000)

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
        return {"success": True, "url_vk": "NO_SUBMIT", "error": ""}

    # Click "Publish" button
    print("  Publishing...")
    try:
        pub_btn = page.locator('button:has-text("Publish"), [class*="publish"]').first
        pub_btn.click(timeout=5000)
    except Exception:
        return {"success": False, "url_vk": "", "error": "Could not find Publish button"}

    page.wait_for_timeout(5000)
    print("  Wall post created")
    return {"success": True, "url_vk": "UPLOADED", "error": ""}


# ── X.com Upload (Playwright) ─────────────────────────────────
X_CHAR_LIMIT = 280


def build_x_post_text(title, keywords_str):
    """Build a tweet from title + hashtags, fitting within 280 characters."""
    text = title.strip()
    if not keywords_str:
        return text[:X_CHAR_LIMIT]
    tags = [k.strip() for k in keywords_str.split(",") if k.strip()]
    for tag in tags:
        hashtag = "#" + tag.replace(" ", "")
        candidate = text + " " + hashtag
        if len(candidate) <= X_CHAR_LIMIT:
            text = candidate
        else:
            break
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
                document.execCommand('insertText', false, text);
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


# ── Facebook Upload ──────────────────────────────────────────────
def upload_to_fb(page, caption, image_path, no_submit=False):
    """
    Post a photo with caption to Facebook personal timeline via browser automation.
    Flow: home → open composer → attach photo → write caption → Next → Post.
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
    page.wait_for_timeout(5000)

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
        print(f"    Topmost textbox: {marked}")

        if marked.get("found"):
            textbox = page.locator('[data-fb-caption="true"]')
            textbox.fill(caption, timeout=5000)
            actual = textbox.inner_text(timeout=2000)
            print(f"    Caption filled: '{actual[:60]}'")
        else:
            print(f"    WARNING: No visible textbox found")
    except Exception as e:
        print(f"    WARNING: Could not write caption: {e}")

    page.wait_for_timeout(1000)

    if no_submit:
        print("  --no-submit: skipping post")
        return {"success": True, "url_fb": "NO_SUBMIT", "error": ""}

    # Submit — Facebook uses React event delegation, so dispatch_event doesn't work.
    # Use Playwright's .click(force=True) to bypass overlay interception while still
    # generating real pointer events that React can detect.
    print("  Posting...")
    try:
        # Click "Next" button (force=True bypasses overlay interception)
        next_btn = page.locator('[aria-label="Next"]')
        if next_btn.count() > 0 and next_btn.first.is_visible():
            next_btn.first.click(force=True, timeout=5000)
            print("    Clicked Next")
            page.wait_for_timeout(3000)

        # Try multiple possible labels for the submit button
        post_btn = None
        for label in ["Post", "Share", "Share now", "Publish", "Next"]:
            candidate = page.locator(f'[aria-label="{label}"]')
            if candidate.count() > 0 and candidate.first.is_visible():
                post_btn = candidate.first
                print(f"    Found submit button: '{label}'")
                break

        if post_btn:
            post_btn.click(force=True, timeout=5000)
            print("    Clicked submit")
        else:
            # Log available buttons for debugging
            buttons = page.evaluate("""() => {
                const btns = document.querySelectorAll('[role="button"]');
                const results = [];
                for (const b of btns) {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        const label = b.getAttribute('aria-label') || b.textContent?.trim().substring(0, 40);
                        if (label) results.push(label);
                    }
                }
                return results.slice(0, 15);
            }""")
            return {"success": False, "url_fb": "", "error": f"Could not find Post/Share button. Available: {buttons}"}
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
    print(f"  Adding {len(tags)} tags...")
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
        for tag in tags:
            page.keyboard.type(tag, delay=50)
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)
        print(f"    Tags entered: {len(tags)}")
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
            // Title: input[name="title"]
            const titleEl = document.querySelector('input[name="title"]');
            const title = titleEl ? titleEl.value.trim() : '';
            // Tags: count elements that look like tag chips (buttons inside tag area)
            // Try multiple selectors for tag chips
            let tagCount = document.querySelectorAll('[class*="tag-item"], [class*="tagItem"], [data-tag]').length;
            if (tagCount === 0) {
                // Fallback: count small buttons near the tag input area
                const allBtns = document.querySelectorAll('button');
                const tagBtns = Array.from(allBtns).filter(b => {
                    const rect = b.getBoundingClientRect();
                    return rect.width > 20 && rect.width < 200 && rect.height > 15 && rect.height < 40
                        && b.textContent.trim().length > 0 && b.textContent.trim().length < 30;
                });
                // Subtract known buttons (Submit, Save, etc)
                tagCount = Math.max(0, tagBtns.length - 10);
            }
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
                        if url_500px and url_500px not in ("NO_SUBMIT", "UPLOADED"):
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
                        if url_35p and url_35p not in ("NO_SUBMIT", "UPLOADED"):
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

                        try:
                            result_vk = upload_to_vk(page, desc_full, image_path, args.no_submit)
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
                        if url_vk and url_vk not in ("NO_SUBMIT",):
                            save_row_update(args.csv, row["upload_id"], {"url_vk": url_vk})

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

                # ── FB (between X and DA) ───────────────────────
                if "FB" in platforms:
                    already = row.get("url_fb", "").strip()
                    if already:
                        print(f"\n  FB: already uploaded ({already}) — skipping")
                    else:
                        print(f"\n  ── Facebook Upload ──")
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
                                fb_caption = row.get("title", "")
                                print(f"    Caption: {fb_caption}")
                                print(f"    Using safe image (NSFW flag set)")
                                try:
                                    result_fb = upload_to_fb(page, fb_caption, fb_image_path, args.no_submit)
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

                            fb_caption = row.get("title", "")
                            print(f"    Caption: {fb_caption}")

                            try:
                                result_fb = upload_to_fb(page, fb_caption, image_path, args.no_submit)
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
                        updates["error_log"] = "; ".join(errors)[:200]
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
