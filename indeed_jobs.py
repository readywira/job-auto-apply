#!/usr/bin/env python3
"""
Indeed Saved Jobs Fetcher — Benjamin's Job Application Pipeline
Logs into Indeed, retrieves saved jobs, scores them with GPT,
generates cover letters + PDFs, and pushes to Airtable.

Usage:
    python indeed_jobs.py              # full pipeline (fetch → score → PDFs → Airtable)
    python indeed_jobs.py --fetch-only # fetch and save to JSON, no PDFs/Airtable
    python indeed_jobs.py --dry-run    # score + cover letters, skip PDFs + Airtable
    python indeed_jobs.py --headless   # run browser in headless mode (no window)
    python indeed_jobs.py --min 0.75   # minimum score threshold (default: 0.70)

Credentials:
    Stored in auth-profiles.json → profiles["indeed:default"]
    {"email": "...", "password": "..."}
    OR use session cookies (saved automatically after first login).

Session:
    Cookies saved to ~/.openclaw/workspace/skills/job-auto-apply/indeed_session.json
    Reused on subsequent runs to avoid re-login.
"""

import json, os, sys, re, time, urllib.request
from datetime import datetime

SKILL_DIR    = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply")
AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
SESSION_FILE = os.path.join(SKILL_DIR, "indeed_session.json")
OUTPUT_BASE  = os.path.expanduser("~/job_applications")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")
TODAY        = datetime.now().strftime("%Y-%m-%d")

MIN_SCORE  = float(next((a for a in sys.argv[1:] if a.replace('.','',1).isdigit()), "0.70"))
HEADLESS   = "--headless" in sys.argv
DRY_RUN    = "--dry-run"  in sys.argv
FETCH_ONLY = "--fetch-only" in sys.argv

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

os.makedirs(os.path.join(OUTPUT_BASE, TODAY, "pdfs"), exist_ok=True)


# ── Load auth ─────────────────────────────────────────────────────────────────
def load_auth():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    profiles = raw.get("profiles", {})

    # OpenAI key
    openai_key = profiles.get("openai:default", {}).get("key", "")

    # Indeed credentials
    indeed = profiles.get("indeed:default", {})
    email  = indeed.get("email", "")
    password = indeed.get("password", "")

    return openai_key, email, password


# ── GPT helpers ───────────────────────────────────────────────────────────────
def gpt(openai_key, prompt, max_tokens=500):
    data = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {openai_key}",
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def score_job(openai_key, profile_summary, job):
    prompt = f"""Score this job match for the candidate. Return ONLY valid JSON (no markdown).

CANDIDATE:
{profile_summary}

JOB:
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
Description: {job['description'][:2000]}

Return:
{{
  "score": <0.0-1.0>,
  "match_reasons": ["reason1", "reason2"],
  "gaps": ["gap1"],
  "salary_ok": true
}}"""
    try:
        raw     = gpt(openai_key, prompt, max_tokens=200)
        raw     = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        scoring = json.loads(raw)
        return float(scoring.get("score", 0)), scoring
    except Exception as e:
        return 0.0, {"score": 0, "match_reasons": [], "gaps": [str(e)], "salary_ok": False}


def write_cover_letter(openai_key, profile_summary, name, email, phone, job):
    prompt = f"""Write a concise, professional cover letter for this job application.

CANDIDATE:
{profile_summary}

JOB:
Title: {job['title']}
Company: {job['company']}
Description: {job['description'][:2000]}

Guidelines:
- 3 short paragraphs (opening, skills match, closing)
- Specific to this role and company
- End with: {name} | {email} | {phone}
- Do NOT use placeholder brackets like [Your Name]"""
    return gpt(openai_key, prompt, max_tokens=500)


# ── Indeed session management ─────────────────────────────────────────────────
def load_cookies():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            return json.load(f)
    return []


def save_cookies(context):
    cookies = context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)


def is_logged_in(page):
    """Check if we're currently logged into Indeed."""
    try:
        page.goto("https://www.indeed.com/account/login", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        # If we get redirected away from login page, we're already logged in
        return "login" not in page.url and "signin" not in page.url
    except Exception:
        return False


def login_indeed(page, email, password):
    """Log into Indeed with email/password."""
    print("    Logging into Indeed...")
    page.goto("https://secure.indeed.com/auth", wait_until="domcontentloaded", timeout=20000)
    time.sleep(2)

    # Enter email
    try:
        email_input = page.wait_for_selector("input[name='__email'], input[type='email'], #ifl-InputFormField-3", timeout=10000)
        email_input.fill(email)
        page.keyboard.press("Enter")
        time.sleep(2)
    except Exception as e:
        print(f"    Email field not found: {e}")
        return False

    # Enter password
    try:
        pwd_input = page.wait_for_selector("input[type='password'], #ifl-InputFormField-7", timeout=10000)
        pwd_input.fill(password)
        page.keyboard.press("Enter")
        time.sleep(3)
    except Exception as e:
        print(f"    Password field not found: {e}")
        return False

    # Check if login succeeded
    time.sleep(2)
    if "indeed.com" in page.url and "auth" not in page.url and "login" not in page.url:
        print("    Logged in successfully")
        return True
    else:
        print(f"    Login result unclear — current URL: {page.url}")
        # Try continuing anyway
        return True


# ── Indeed saved jobs scraper ─────────────────────────────────────────────────
def scrape_saved_jobs(page):
    """Navigate to saved jobs and extract all listings."""
    saved_jobs_urls = [
        "https://www.indeed.com/my-jobs",
        "https://www.indeed.com/profile/saved-jobs",
    ]

    jobs = []
    for url in saved_jobs_urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)

            # Check if we ended up on the saved jobs page
            if "my-jobs" in page.url or "saved" in page.url:
                break
        except Exception:
            continue

    print(f"    On page: {page.url}")

    # Scroll to load all saved jobs (lazy loading)
    prev_count = 0
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
        # Count job cards
        count = page.evaluate("document.querySelectorAll('[data-testid=\"job-card\"], .jobCard, .job_seen_beacon').length")
        if count == prev_count:
            break
        prev_count = count

    # Extract job cards
    job_cards = page.query_selector_all(
        '[data-testid="job-card"], .jobCard, .job_seen_beacon, '
        '[class*="jobCard"], [class*="tapItem"], [class*="result"]'
    )

    print(f"    Found {len(job_cards)} job cards")

    for card in job_cards:
        try:
            # Title
            title_el = (card.query_selector('[data-testid="jobTitle"] a') or
                       card.query_selector('h2 a[data-jk]') or
                       card.query_selector('.jobTitle a') or
                       card.query_selector('a[id^="job_"]'))
            title = title_el.inner_text().strip() if title_el else ""
            job_url = title_el.get_attribute("href") if title_el else ""
            if job_url and job_url.startswith("/"):
                job_url = f"https://www.indeed.com{job_url}"

            # Company
            company_el = (card.query_selector('[data-testid="company-name"]') or
                         card.query_selector('.companyName') or
                         card.query_selector('[class*="companyName"]'))
            company = company_el.inner_text().strip() if company_el else ""

            # Location
            location_el = (card.query_selector('[data-testid="text-location"]') or
                          card.query_selector('.companyLocation') or
                          card.query_selector('[class*="companyLocation"]'))
            location = location_el.inner_text().strip() if location_el else ""

            # Salary (optional)
            salary_el = (card.query_selector('[data-testid="attribute_snippet_testid"]') or
                        card.query_selector('.salaryOnly') or
                        card.query_selector('[class*="salary"]'))
            salary = salary_el.inner_text().strip() if salary_el else "Not listed"

            if title and company:
                jobs.append({
                    "title":       title,
                    "company":     company,
                    "location":    location,
                    "salary":      salary,
                    "apply_url":   job_url,
                    "platform":    "Indeed (Saved)",
                    "description": "",  # will be fetched separately
                })
        except Exception:
            continue

    return jobs


def fetch_job_description(page, job_url, timeout=10000):
    """Navigate to job page and extract description."""
    if not job_url:
        return ""
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=timeout)
        time.sleep(1.5)
        desc_el = (page.query_selector('#jobDescriptionText') or
                  page.query_selector('[data-testid="jobDescriptionText"]') or
                  page.query_selector('.jobsearch-jobDescriptionText'))
        if desc_el:
            return desc_el.inner_text().strip()[:3000]
    except Exception:
        pass
    return ""


# ── Main pipeline ─────────────────────────────────────────────────────────────
def main():
    openai_key, indeed_email, indeed_password = load_auth()

    if not indeed_email or not indeed_password:
        print("ERROR: Indeed credentials not found in auth-profiles.json")
        print('Add: profiles["indeed:default"] = {"email": "...", "password": "..."}')
        sys.exit(1)

    # Load profile
    profile_raw = json.load(open(PROFILE_PATH))["profile"]
    p = profile_raw
    NAME  = p["personal"]["full_name"]
    EMAIL = p["personal"]["email"]
    PHONE = p["personal"]["phone"]
    TITLE = p["experience"]["current_title"]
    YOE   = p["experience"]["years_total"]
    SKILLS = (
        ", ".join(p["skills"]["programming_languages"]) + ", " +
        ", ".join(p["skills"]["frameworks"]) + ", " +
        ", ".join(p["skills"]["tools"])
    )
    CERTS = ", ".join(p["skills"].get("certifications", []))

    PROFILE_SUMMARY = f"""Name: {NAME}
Current Title: {TITLE}
Years of Experience: {YOE}
Skills: {SKILLS}
Certifications: {CERTS}
Work Authorization: Authorized, no sponsorship needed""".strip()

    print(f"\n{'='*60}")
    print(f"  INDEED SAVED JOBS — {TODAY}")
    print(f"  Min score: {MIN_SCORE}")
    print(f"{'='*60}")

    # ── Launch browser ────────────────────────────────────────────────────────
    WSL2_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",    # WSL2: /dev/shm is small
        "--no-sandbox",               # WSL2: no kernel sandbox
        "--disable-setuid-sandbox",
    ]

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=HEADLESS, args=WSL2_ARGS)
        except Exception as e:
            if "libnspr4" in str(e) or "shared libraries" in str(e) or "TargetClosedError" in str(e):
                print("\nERROR: Missing system libraries for Chromium.")
                print("Fix with: sudo apt-get install -y libnspr4 libnss3 libasound2t64")
                print("Then re-run this script.")
                sys.exit(1)
            raise
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        # Restore saved cookies
        saved_cookies = load_cookies()
        if saved_cookies:
            try:
                context.add_cookies(saved_cookies)
                print("  Restored saved session cookies")
            except Exception:
                pass

        page = context.new_page()

        # ── Login if needed ───────────────────────────────────────────────────
        print("\n  Checking Indeed login...")
        page.goto("https://www.indeed.com", wait_until="domcontentloaded", timeout=20000)
        time.sleep(2)

        logged_in = False
        # Check for login indicators
        if context.cookies():
            for c in context.cookies():
                if c.get("name") in ("CTK", "CSRF_TOKEN", "LI_AT", "CF_bm"):
                    pass
            # Try navigating to my-jobs to check
            page.goto("https://www.indeed.com/my-jobs", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            if "my-jobs" in page.url:
                logged_in = True
                print("  Already logged in (session cookies valid)")

        if not logged_in:
            logged_in = login_indeed(page, indeed_email, indeed_password)
            if logged_in:
                save_cookies(context)
                print("  Session cookies saved")

        # ── Scrape saved jobs ─────────────────────────────────────────────────
        print("\n  Scraping saved jobs...")
        raw_jobs = scrape_saved_jobs(page)
        print(f"  Found {len(raw_jobs)} saved jobs")

        if not raw_jobs:
            print("  No saved jobs found. Make sure you have jobs saved on Indeed.")
            browser.close()
            return

        # ── Fetch descriptions ────────────────────────────────────────────────
        print("\n  Fetching job descriptions...")
        for i, job in enumerate(raw_jobs):
            if job["apply_url"]:
                print(f"  [{i+1}/{len(raw_jobs)}] {job['company']} — {job['title']}...", end=" ", flush=True)
                job["description"] = fetch_job_description(page, job["apply_url"])
                print(f"({len(job['description'])} chars)")
            time.sleep(1.0)

        browser.close()

    # Save raw fetch to JSON
    raw_path = os.path.join(OUTPUT_BASE, f"{TODAY}_indeed_raw.json")
    with open(raw_path, "w") as f:
        json.dump(raw_jobs, f, indent=2, default=str)
    print(f"\n  Raw jobs saved → {raw_path}")

    if FETCH_ONLY:
        print(f"  (--fetch-only: skipping scoring)")
        return

    # ── Score jobs ────────────────────────────────────────────────────────────
    print(f"\n  Scoring {len(raw_jobs)} jobs (min={MIN_SCORE})...")
    scored_jobs = []

    for i, job in enumerate(raw_jobs):
        print(f"  [{i+1}/{len(raw_jobs)}] {job['title']} @ {job['company']}...", end=" ", flush=True)
        score, scoring = score_job(openai_key, PROFILE_SUMMARY, job)
        print(f"score={score:.2f}")

        if score >= MIN_SCORE:
            scored_jobs.append({
                **job,
                "score":   score,
                "scoring": scoring,
                "match_reasons": "; ".join(scoring.get("match_reasons", [])),
                "skill_gaps":    "; ".join(scoring.get("gaps", [])),
            })
        time.sleep(0.5)

    scored_jobs.sort(key=lambda x: x["score"], reverse=True)
    print(f"  Matched {len(scored_jobs)} / {len(raw_jobs)} jobs above threshold")

    if not scored_jobs:
        print("  No jobs above threshold. Done.")
        return

    # ── Generate cover letters ────────────────────────────────────────────────
    print(f"\n  Generating cover letters...")
    for entry in scored_jobs:
        try:
            entry["cover_letter"] = write_cover_letter(
                openai_key, PROFILE_SUMMARY, NAME, EMAIL, PHONE, entry
            )
        except Exception as e:
            entry["cover_letter"] = f"[Cover letter generation failed: {e}]"
        print(f"    ✓ {entry['company']} — {entry['title']}")
        time.sleep(0.5)

    # Save scored JSON
    scored_path = os.path.join(OUTPUT_BASE, f"{TODAY}_indeed_matches.json")
    with open(scored_path, "w") as f:
        json.dump(scored_jobs, f, indent=2, default=str)
    print(f"\n  Scored matches → {scored_path}")

    if DRY_RUN:
        print("  (--dry-run: skipping PDFs and Airtable)")
        return

    # ── Generate PDFs ─────────────────────────────────────────────────────────
    print(f"\n  Generating PDFs...")
    sys.path.insert(0, SKILL_DIR)
    try:
        from pdf_generator import generate_resume, generate_cover_letter
        resume_path = generate_resume()
        for entry in scored_jobs:
            cl_text = entry.get("cover_letter", "")
            if cl_text:
                entry["cover_pdf"]  = generate_cover_letter(entry["company"], entry["title"], cl_text)
                entry["resume_pdf"] = resume_path
    except Exception as e:
        print(f"  PDF generation failed: {e}")

    # ── Sync to Airtable ──────────────────────────────────────────────────────
    print(f"\n  Syncing to Airtable...")
    try:
        from airtable_sync import (
            load_auth as at_load_auth,
            ensure_base,
            fetch_existing_urls,
            batch_create_records,
        )
        at_key, at_base_id, at_table_id = at_load_auth()
        at_base_id, at_table_id = ensure_base(at_key, at_base_id, at_table_id)
        existing_urls = fetch_existing_urls(at_key, at_base_id, at_table_id)

        at_jobs = [{
            "title":         e.get("title", ""),
            "company":       e.get("company", ""),
            "score":         e.get("score", 0),
            "location":      e.get("location", ""),
            "salary":        e.get("salary", ""),
            "platform":      e.get("platform", "Indeed (Saved)"),
            "match_reasons": e.get("match_reasons", ""),
            "skill_gaps":    e.get("skill_gaps", ""),
            "apply_url":     e.get("apply_url", ""),
            "cover_letter":  e.get("cover_letter", ""),
            "resume_pdf":    e.get("resume_pdf", ""),
            "cover_pdf":     e.get("cover_pdf", ""),
        } for e in scored_jobs]

        created_pairs = batch_create_records(at_key, at_base_id, at_jobs, existing_urls)
        print(f"  Airtable: {len(created_pairs)} new records created")
    except Exception as e:
        print(f"  Airtable sync failed: {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Indeed Saved Jobs — Done")
    print(f"  Saved jobs found:  {len(raw_jobs)}")
    print(f"  Good matches:      {len(scored_jobs)}")
    print(f"  TOP MATCHES:")
    for e in scored_jobs[:5]:
        print(f"    {e['score']:.0%}  {e['title']} @ {e['company']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
