#!/usr/bin/env python3
"""
LinkedIn Easy Apply Automation — Benjamin's Job Application Pipeline
Fetches "Ready to Apply" jobs from Airtable and submits via LinkedIn Easy Apply.

Usage:
    python linkedin_apply.py               # apply to all ready jobs
    python linkedin_apply.py --dry-run     # fill forms but don't submit
    python linkedin_apply.py --limit 5     # apply to at most 5 jobs

Requires:
    pip install playwright && playwright install chromium

Auth (auth-profiles.json):
    profiles["linkedin:default"]["email"]
    profiles["linkedin:default"]["password"]
    profiles["airtable:default"]["key"] + base_id
"""

import json, os, sys, time, re
from datetime import datetime

AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
SESSION_DIR  = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply")
SESSION_FILE = os.path.join(SESSION_DIR, "linkedin_session.json")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")

os.makedirs(SESSION_DIR, exist_ok=True)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# Import airtable helpers from sibling module
sys.path.insert(0, os.path.dirname(__file__))
from airtable_sync import (
    load_auth as at_load_auth,
    fetch_records_by_status,
    update_status,
    AT_BASE_URL,
    TABLE_NAME,
)
import urllib.parse


# ── Auth ─────────────────────────────────────────────────────────────────────
def load_linkedin_creds():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    li = raw.get("profiles", {}).get("linkedin:default", {})
    email    = li.get("email", "")
    password = li.get("password", "")
    if not email or not password:
        print("ERROR: LinkedIn credentials missing.")
        print("Add to auth-profiles.json: profiles['linkedin:default']['email'] and ['password']")
        sys.exit(1)
    return email, password


def load_profile():
    with open(PROFILE_PATH) as f:
        return json.load(f)["profile"]


# ── LinkedIn login ────────────────────────────────────────────────────────────
def login(page, email, password):
    """Log in to LinkedIn, or reuse existing session."""
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    time.sleep(2)

    # Check if already logged in
    if "feed" in page.url or "jobs" in page.url:
        print("  ✓ Session active — already logged in")
        return True

    print("  Logging in to LinkedIn...")
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    time.sleep(1.5)

    page.fill("#username", email)
    page.fill("#password", password)
    page.click('button[type="submit"]')
    time.sleep(4)

    if "checkpoint" in page.url or "challenge" in page.url:
        print("\n  ⚠ LinkedIn requires verification (CAPTCHA/2FA).")
        print("  Complete it in the browser window, then press Enter...")
        input("  [Press Enter when done] ")
        time.sleep(2)

    if "feed" in page.url or "jobs" in page.url:
        print("  ✓ Logged in successfully")
        return True

    print(f"  ⚠ Login may have failed. Current URL: {page.url}")
    return False


def save_session(context):
    """Save full browser storage state (cookies + localStorage + sessionStorage)."""
    storage = context.storage_state()
    with open(SESSION_FILE, "w") as f:
        json.dump(storage, f, indent=2)
    print(f"  ✓ Session saved to {SESSION_FILE}")


# ── Easy Apply form handling ─────────────────────────────────────────────────
def fill_easy_apply(page, profile, dry_run=False):
    """
    Navigate the multi-step Easy Apply form.
    Returns True on successful submission.
    """
    per  = profile["personal"]
    name = per["full_name"]
    phone = per["phone"]
    email = per["email"]

    # Click "Easy Apply" button — try multiple selectors
    EASY_APPLY_SELECTOR = (
        'button:has-text("Easy Apply"), '
        'button[aria-label*="Easy Apply"], '
        '.jobs-apply-button--top-card button, '
        '.jobs-s-apply button'
    )
    try:
        easy_btn = page.locator(EASY_APPLY_SELECTOR).first
        easy_btn.wait_for(timeout=12000)
        easy_btn.scroll_into_view_if_needed()
        time.sleep(0.5)
        easy_btn.click()
        time.sleep(2.5)
    except PWTimeout:
        # Debug: dump ALL button texts (visible or not) to diagnose
        btns = page.locator("button")
        all_btns = []
        for i in range(min(btns.count(), 40)):
            try:
                t = (btns.nth(i).inner_text() or "").strip()[:50]
                if t:
                    all_btns.append(t)
            except Exception:
                pass
        print(f"    ⚠ No Easy Apply button. All buttons: {all_btns}")
        return False

    step = 0
    MAX_STEPS = 12

    while step < MAX_STEPS:
        step += 1
        time.sleep(1.5)

        # ── Fill visible form fields ─────────────────────────────────────────
        _fill_text_fields(page, phone, email, profile)

        # Check for file upload (resume)
        _handle_resume_upload(page, profile)

        # Check for work authorization / sponsorship questions
        _answer_auth_questions(page, profile)

        # ── Determine which button to press ──────────────────────────────────
        submit_btn = page.locator('button:has-text("Submit application")').first
        next_btn   = page.locator('button:has-text("Next")').first
        review_btn = page.locator('button:has-text("Review")').first

        if submit_btn.is_visible():
            if dry_run:
                print(f"    [DRY RUN] Would click Submit on step {step}")
                # Close modal
                _close_modal(page)
                return True
            else:
                submit_btn.click()
                time.sleep(2)
                print(f"    ✓ Submitted on step {step}")
                # Dismiss confirmation
                _close_modal(page)
                return True
        elif review_btn.is_visible():
            review_btn.click()
        elif next_btn.is_visible():
            next_btn.click()
        else:
            # Try to find any continuation button
            btns = page.locator('button[aria-label*="Continue"], button[aria-label*="Next"]')
            if btns.count() > 0:
                btns.first.click()
            else:
                print(f"    ⚠ No navigation button found on step {step}")
                _close_modal(page)
                return False

    print("    ⚠ Exceeded max steps — bailing out")
    _close_modal(page)
    return False


def _fill_text_fields(page, phone, email, profile):
    """Fill common text inputs in Easy Apply forms."""
    per = profile["personal"]
    mappings = [
        # Phone
        ('input[id*="phoneNumber"], input[name*="phone"], '
         'input[placeholder*="phone" i], input[aria-label*="phone" i]', phone),
        # Email
        ('input[id*="email"], input[name*="email"], '
         'input[placeholder*="email" i]', email),
        # City
        ('input[id*="city" i], input[placeholder*="city" i]', per["location"]["city"]),
        # LinkedIn URL
        ('input[id*="linkedin" i], input[placeholder*="linkedin" i]',
         per.get("linkedin_url", "")),
        # Website / portfolio
        ('input[id*="website" i], input[id*="portfolio" i]',
         per.get("portfolio_url") or per.get("github_url", "")),
    ]
    for selector, value in mappings:
        if not value:
            continue
        try:
            els = page.locator(selector)
            for i in range(els.count()):
                el = els.nth(i)
                if el.is_visible() and el.is_enabled():
                    current = el.input_value() or ""
                    if not current:  # only fill if empty
                        el.fill(value)
        except Exception:
            pass

    # Salary fields — fill with minimum expectation
    sal_min = profile.get("preferences", {}).get("salary_expectations", {}).get("minimum", 0)
    if sal_min:
        try:
            sal_els = page.locator(
                'input[id*="salary" i], input[placeholder*="salary" i], '
                'input[aria-label*="salary" i]'
            )
            for i in range(sal_els.count()):
                el = sal_els.nth(i)
                if el.is_visible() and el.is_enabled():
                    current = el.input_value() or ""
                    if not current:
                        el.fill(str(sal_min))
        except Exception:
            pass


def _handle_resume_upload(page, profile):
    """Upload resume PDF if a file upload field is visible."""
    resume_paths = []
    output_base  = os.path.expanduser("~/job_applications")
    today        = datetime.now().strftime("%Y-%m-%d")
    resume_pdf   = os.path.join(output_base, today, "pdfs", "resume.pdf")
    if os.path.exists(resume_pdf):
        resume_paths.append(resume_pdf)

    if not resume_paths:
        return

    try:
        upload_els = page.locator('input[type="file"]')
        for i in range(upload_els.count()):
            el = upload_els.nth(i)
            if el.is_visible() or True:  # file inputs may be hidden
                try:
                    el.set_input_files(resume_paths[0])
                    time.sleep(1)
                    break
                except Exception:
                    pass
    except Exception:
        pass


def _answer_auth_questions(page, profile):
    """Auto-answer common work authorization and experience questions."""
    wa  = profile.get("work_authorization", {})
    yoe = profile.get("experience", {}).get("years_total", 0)

    # Authorized to work in US → Yes
    _click_radio_for_label(page, ["authorized to work", "work authorization", "eligible to work"],
                           value="Yes")

    # Visa sponsorship → No
    _click_radio_for_label(page, ["require sponsorship", "need sponsorship", "visa sponsorship"],
                           value="No")

    # Years of experience → fill numeric
    try:
        yoe_els = page.locator(
            'input[id*="experience" i], input[aria-label*="years of experience" i],'
            'input[placeholder*="years" i]'
        )
        for i in range(yoe_els.count()):
            el = yoe_els.nth(i)
            if el.is_visible() and el.is_enabled():
                current = el.input_value() or ""
                if not current:
                    el.fill(str(yoe))
    except Exception:
        pass

    # Dropdowns: select "Yes" for work auth / "No" for sponsorship
    _answer_select_questions(page)


def _click_radio_for_label(page, keywords, value="Yes"):
    """Find a question whose text matches keywords and click the Yes/No radio."""
    try:
        labels = page.locator("label, legend, span.t-14")
        for i in range(min(labels.count(), 30)):
            label = labels.nth(i)
            try:
                text = (label.inner_text() or "").lower()
            except Exception:
                continue
            if any(kw in text for kw in keywords):
                # Find radio/button with value text near this label
                parent = label.locator("xpath=../..")
                options = parent.locator(f'label:has-text("{value}"), '
                                         f'input[value="{value.lower()}"]')
                if options.count() > 0:
                    options.first.click()
                    time.sleep(0.3)
                    return
    except Exception:
        pass


def _answer_select_questions(page):
    """Handle dropdown selects for common questions."""
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            if not sel.is_visible():
                continue
            # Get label text for context
            sel_id = sel.get_attribute("id") or ""
            label  = page.locator(f'label[for="{sel_id}"]')
            label_text = ""
            if label.count() > 0:
                label_text = (label.first.inner_text() or "").lower()

            opts = sel.locator("option")
            opt_texts = [opts.nth(j).inner_text().strip() for j in range(opts.count())]

            if any(kw in label_text for kw in ["authorized", "eligible", "work in us"]):
                if "Yes" in opt_texts:
                    sel.select_option("Yes")
            elif any(kw in label_text for kw in ["sponsor", "visa"]):
                if "No" in opt_texts:
                    sel.select_option("No")
    except Exception:
        pass


def _close_modal(page):
    """Close the Easy Apply modal if open."""
    try:
        dismiss = page.locator(
            'button[aria-label="Dismiss"], '
            'button[aria-label="Close the popup dialog"], '
            'button.artdeco-modal__dismiss'
        )
        if dismiss.count() > 0 and dismiss.first.is_visible():
            dismiss.first.click()
            time.sleep(0.5)
            # Confirm discard if prompted
            discard = page.locator('button:has-text("Discard")')
            if discard.count() > 0 and discard.first.is_visible():
                discard.first.click()
    except Exception:
        pass


# ── Main application loop ─────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv
    limit   = None
    if "--limit" in sys.argv:
        idx   = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    print(f"\n{'='*60}")
    print(f"  LINKEDIN EASY APPLY — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}")

    # Load credentials
    li_email, li_password = load_linkedin_creds()
    at_key, at_base_id, at_table_id = at_load_auth()
    profile = load_profile()

    if not at_base_id:
        print("ERROR: No Airtable base_id found. Run airtable_sync.py --setup first.")
        sys.exit(1)

    # Fetch jobs ready to apply
    print("\n  Fetching jobs from Airtable (Status = Ready to Apply)...")
    records = fetch_records_by_status(at_key, at_base_id, "Ready to Apply")
    if limit:
        records = records[:limit]

    if not records:
        print("  No jobs marked 'Ready to Apply' in Airtable.")
        print("  Open Airtable, review the jobs, and set Status → Ready to Apply.")
        return

    print(f"  Found {len(records)} jobs to apply to\n")

    applied  = 0
    skipped  = 0
    failed   = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=50)

        # storage_state must be passed at context creation time to restore localStorage
        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            )
        )
        if os.path.exists(SESSION_FILE):
            ctx_kwargs["storage_state"] = SESSION_FILE
            print(f"  ✓ Loaded session from {SESSION_FILE}")

        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        # Login (will reuse session if valid, otherwise log in fresh)
        logged_in = login(page, li_email, li_password)
        if not logged_in:
            print("  ERROR: Could not log in to LinkedIn. Aborting.")
            browser.close()
            return

        save_session(context)

        for rec in records:
            fields    = rec.get("fields", {})
            record_id = rec["id"]
            company   = fields.get("Company", "?")
            title     = fields.get("Job Title", "?")
            apply_url = fields.get("Apply URL", "")

            print(f"\n  [{applied+skipped+failed+1}/{len(records)}] "
                  f"{title} @ {company}")

            if not apply_url:
                print("    ⚠ No Apply URL — skipping")
                skipped += 1
                continue

            if "linkedin.com" not in apply_url:
                print(f"    ⚠ Not a LinkedIn URL: {apply_url[:60]}")
                skipped += 1
                continue

            # Navigate to job — strip UTM params, wait for full render
            try:
                clean_url = apply_url.split("?")[0]
                page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
                # Wait for LinkedIn React content to finish rendering
                time.sleep(5)
                # Scroll to top to ensure apply button in sticky header is visible
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1)
            except Exception as e:
                print(f"    ⚠ Navigation failed: {e}")
                failed += 1
                continue

            # Apply
            success = fill_easy_apply(page, profile, dry_run=dry_run)

            if success:
                applied += 1
                if not dry_run:
                    update_status(
                        at_key, at_base_id, record_id,
                        status="Submitted",
                        applied_date=datetime.now().strftime("%Y-%m-%d"),
                        notes=f"Applied via LinkedIn Easy Apply on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    )
                    print(f"    ✓ Status → Submitted in Airtable")
                else:
                    print(f"    [DRY RUN] Would update Airtable → Submitted")
            else:
                failed += 1

            # Rate limit between applications
            print(f"    Waiting 3 seconds...")
            time.sleep(3)

        save_session(context)
        browser.close()

    print(f"\n{'='*60}")
    print(f"  SUMMARY {'(DRY RUN)' if dry_run else ''}")
    print(f"  Applied:  {applied}")
    print(f"  Skipped:  {skipped}")
    print(f"  Failed:   {failed}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
