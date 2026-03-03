#!/usr/bin/env python3
"""
Playwright-based DeviantArt upload automation.
Reads upload_queue.csv, fills the Sta.sh submission form, and publishes to DA.

Usage:
    python upload.py                          # all Approved DA rows
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

COLS = [
    "upload_id", "scheduled_date", "scheduled_time",
    "stash_url_nsfw", "stash_url_safe", "title", "caption", "keywords",
    "da_nsfw_flag", "category_500px", "category_35p", "caption_fb",
    "platforms", "status", "upload_timestamp",
    "da_deviation_url", "url_500px", "url_35p", "url_fb",
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
        description="Upload approved photos to DeviantArt via Playwright"
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

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


# ── Row filtering ─────────────────────────────────────────────
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

        # Must have DA in platforms
        platforms = [p.strip() for p in row.get("platforms", "").split(",")]
        if "DA" not in platforms:
            if target_id:
                print(f"WARNING: {row['upload_id']} does not have DA in platforms — skipping")
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


# ── Dry run ───────────────────────────────────────────────────
def print_dry_run(rows, config):
    print("\nDRY RUN — no browser will be launched")
    print("=" * 60)
    for i, row in enumerate(rows):
        desc = build_description(row, config)
        tags = prepare_tags(row.get("keywords", ""))
        groups = parse_groups(row.get("da_groups", ""))
        galleries = row.get("da_gallery", "Featured")

        print(f"\nRow {i + 1}: {row['upload_id']}")
        print(f"  Sta.sh:      {row.get('stash_url_nsfw', '(missing)')}")
        print(f"  Title:       {row.get('title', '(missing)')}")
        print(f"  Tags ({len(tags)}):   {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}")
        print(f"  NSFW:        {row.get('da_nsfw_flag', 'FALSE')}")
        print(f"  Gallery:     {galleries}")
        if groups:
            print(f"  Groups:      {', '.join(f'{g['group']}:{g['folder']}' for g in groups)}")
        print(f"  Description: {desc[:100]}{'...' if len(desc) > 100 else ''}")

    print("\n" + "=" * 60)
    print(f"Would upload {len(rows)} row(s). Run without --dry-run to proceed.")


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

    # Login mode: open browser for DA login, then exit
    if args.login:
        print(f"Opening browser for login (profile: {args.profile})")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(args.profile),
                headless=False,
                args=["--no-first-run", "--no-default-browser-check"],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.goto("https://www.deviantart.com/users/login", wait_until="networkidle", timeout=60000)
            print("Log into DeviantArt in the browser window.")
            print("Press ENTER here when done...")
            input()
            ctx.close()
        print("Login saved. You can now run: python upload.py --no-submit")
        sys.exit(0)

    # Load config
    config = load_config(args.config)

    # Load and filter CSV
    all_rows = load_queue(args.csv)
    target_rows = filter_rows(all_rows, args.row)

    if not target_rows:
        print("No rows to upload.")
        print("Check that: status=Approved, platforms contains DA, and no AUTO fields remain.")
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

        results_summary = []

        try:
            for i, row in enumerate(target_rows):
                print(f"\n{'=' * 60}")
                print(f"[{i + 1}/{len(target_rows)}] {row['upload_id']} — {row.get('title', '?')}")
                print(f"{'=' * 60}")

                desc_full = build_description(row, config)
                tags = prepare_tags(row.get("keywords", ""))
                groups = parse_groups(row.get("da_groups", ""))

                try:
                    result = upload_to_da(page, row, desc_full, tags, groups, args.no_submit)
                except PlaywrightTimeout as e:
                    result = {"success": False, "deviation_url": "", "error": f"Timeout: {e}"}
                except Exception as e:
                    result = {"success": False, "deviation_url": "", "error": f"Unexpected: {e}"}

                # Screenshot on failure
                if not result["success"] and result.get("error"):
                    ts = datetime.now().strftime("%H%M%S")
                    shot_path = SCRIPT_DIR / f"error_{row['upload_id']}_{ts}.png"
                    try:
                        page.screenshot(path=str(shot_path))
                        print(f"  Error screenshot: {shot_path}")
                    except Exception:
                        pass

                # Update CSV
                updates = {
                    "status": "Uploaded" if result["success"] else "Failed",
                    "upload_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                if result.get("deviation_url") and result["deviation_url"] not in ("NO_SUBMIT", "DRY_RUN"):
                    updates["da_deviation_url"] = result["deviation_url"]
                if result.get("error"):
                    # Sanitize: keep first line only to prevent CSV corruption
                    err_msg = result["error"].split("\n")[0][:200]
                    updates["error_log"] = err_msg

                if not args.no_submit:
                    save_row_update(args.csv, row["upload_id"], updates)

                status = "SUCCESS" if result["success"] else "FAILED"
                detail = result.get("deviation_url", "") or result.get("error", "")
                print(f"\n  {status}: {detail}")
                results_summary.append((row["upload_id"], status, detail))

        finally:
            context.close()

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for uid, status, detail in results_summary:
        print(f"  {status}: {uid} — {detail}")
    print(f"\n{len(results_summary)} row(s) processed.")


if __name__ == "__main__":
    main()
