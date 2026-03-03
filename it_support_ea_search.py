#!/usr/bin/env python3
"""
IT Support Fresh Search — with verified LinkedIn Easy Apply
Clears Airtable, searches for IT support roles (≤3 days old),
verifies LinkedIn Easy Apply on-page before selecting, picks top 5 with ≥2 verified EA.

Usage:
    python it_support_ea_search.py
"""

import json, os, sys, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

SKILL_DIR   = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE   = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")
SESSION_FILE = os.path.join(SKILL_DIR, "linkedin_session.json")
OUTPUT_BASE  = os.path.expanduser("~/job_applications")
TODAY        = datetime.now().strftime("%Y-%m-%d")

MIN_SCORE    = 0.70
TARGET_TOTAL = 5
TARGET_LI_EA = 2       # minimum verified LinkedIn Easy Apply jobs
CUTOFF_DT    = datetime.now(timezone.utc) - timedelta(days=3)

# ── Auth ─────────────────────────────────────────────────────────────────────
with open(AUTH_FILE) as f:
    _auth = json.load(f)["profiles"]
OPENAI_KEY  = _auth["openai:default"]["key"]
RAPIDAPI_KEY = _auth["rapidapi:default"]["key"]

with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)["profile"]

# ── Profile summary ───────────────────────────────────────────────────────────
p = PROFILE
NAME     = p["personal"]["full_name"]
EMAIL    = p["personal"]["email"]
PHONE    = p["personal"]["phone"]
LOCATION = f"{p['personal']['location']['city']}, {p['personal']['location']['state']}"
LINKEDIN = p["personal"]["linkedin_url"]
GITHUB   = p["personal"]["github_url"]
TITLE    = p["experience"]["current_title"]
YOE      = p["experience"]["years_total"]
SKILLS   = (", ".join(p["skills"]["programming_languages"]) + ", " +
            ", ".join(p["skills"]["tools"]))
CERTS    = ", ".join(p["skills"].get("certifications", []))
SAL_MIN  = p["preferences"]["salary_expectations"]["minimum"]

PROFILE_SUMMARY = (
    f"Name: {NAME}\n"
    f"Current Title: {TITLE}\n"
    f"Years of Experience: {YOE}\n"
    f"Location: {LOCATION}\n"
    f"Work Preference: remote, hybrid\n"
    f"Min Salary: ${SAL_MIN:,}/yr\n"
    f"Skills: {SKILLS}\n"
    f"Certifications: {CERTS}\n"
    f"Work Authorization: Authorized in US, no sponsorship needed"
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def gpt_json(prompt, max_tokens=250):
    data = json.dumps({
        "model": "gpt-4o-mini",
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": "Bearer " + OPENAI_KEY,
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])


def jsearch(query, pages=1):
    q = urllib.parse.quote(query)
    url = (f"https://jsearch.p.rapidapi.com/search"
           f"?query={q}&page=1&num_pages={pages}&country=us&date_posted=3days")
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
        "x-rapidapi-key":  RAPIDAPI_KEY,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("data", [])


def is_fresh(job):
    """True if the job was posted within 3 days."""
    dt_str = job.get("job_posted_at_datetime_utc")
    if not dt_str:
        return True  # no date = assume fresh
    try:
        posted = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return posted >= CUTOFF_DT
    except Exception:
        return True


def score_job(job):
    """Return (score float, scoring dict). Safe against {} in description."""
    title       = job.get("job_title", "")
    company     = job.get("employer_name", "")
    location    = ("Remote" if job.get("job_is_remote")
                   else (job.get("job_city") or "") + ", " + (job.get("job_state") or ""))
    salary_min  = job.get("job_min_salary") or 0
    salary_max  = job.get("job_max_salary") or 0
    description = (job.get("job_description") or "")[:2500]
    salary_str  = (f"${int(salary_min):,} - ${int(salary_max):,}" if salary_max
                   else "Not listed")

    prompt = (
        "You are a job match evaluator. Score this job for the candidate. "
        "Return ONLY valid JSON with keys: score (0.0-1.0), "
        "match_reasons (list), gaps (list), salary_ok (bool).\n\n"
        "CANDIDATE:\n" + PROFILE_SUMMARY + "\n\n"
        "JOB:\n"
        "Title: " + title + "\n"
        "Company: " + company + "\n"
        "Location: " + location + "\n"
        "Salary: " + salary_str + "\n"
        "Description: " + description
    )
    try:
        scoring = gpt_json(prompt)
        score   = float(scoring.get("score", 0))
        if score > 1.0:
            score = round(score / 10, 2)
        return score, scoring
    except Exception as e:
        return 0.0, {"score": 0, "match_reasons": [], "gaps": [str(e)], "salary_ok": False}


# ── LinkedIn Easy Apply verifier ──────────────────────────────────────────────
_pw_instance = None
_li_browser  = None
_li_context  = None
_li_page     = None

def _init_playwright():
    global _pw_instance, _li_browser, _li_context, _li_page
    if _li_page is not None:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠ Playwright not installed — skipping EA verification")
        return

    print("\n  Starting Playwright for EA verification...")
    _pw_instance = sync_playwright().__enter__()
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
        print("  ✓ LinkedIn session loaded")

    _li_browser = _pw_instance.chromium.launch(
        headless=True,
        args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-setuid-sandbox"]
    )
    _li_context = _li_browser.new_context(**ctx_kwargs)
    _li_page    = _li_context.new_page()
    print("  ✓ Playwright ready")


def _close_playwright():
    global _pw_instance, _li_browser, _li_context, _li_page
    try:
        if _li_browser:
            _li_browser.close()
        if _pw_instance:
            _pw_instance.__exit__(None, None, None)
    except Exception:
        pass
    _pw_instance = _li_browser = _li_context = _li_page = None


EA_SELECTOR = (
    'button:has-text("Easy Apply"), '
    'button[aria-label*="Easy Apply"], '
    '.jobs-apply-button--top-card button, '
    '.jobs-s-apply button'
)

def verify_linkedin_ea(url):
    """
    Navigate to a LinkedIn job page and return True if Easy Apply button is present.
    Returns False if no button, page fails to load, or Playwright not available.
    """
    _init_playwright()
    if _li_page is None:
        return False

    clean_url = url.split("?")[0]
    try:
        _li_page.goto(clean_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(3)
        _li_page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.5)

        ea = _li_page.locator(EA_SELECTOR)
        found = ea.count() > 0
        if found:
            # Double-check: confirm at least one is attached
            for i in range(ea.count()):
                try:
                    ea.nth(i).wait_for(state="attached", timeout=2000)
                    return True
                except Exception:
                    pass
        return False
    except Exception as e:
        print(f"      EA verify error: {e}")
        return False


# ── Airtable helpers ──────────────────────────────────────────────────────────
def _at_req(url, method="GET", data=None, key=None):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def clear_airtable(key, base_id, table_id):
    """Delete all records in the table."""
    print("\n  Clearing Airtable...")
    records = []
    offset = None
    while True:
        url = (f"https://api.airtable.com/v0/{base_id}/{table_id}"
               f"?pageSize=100" + (f"&offset={offset}" if offset else ""))
        d = _at_req(url, key=key)
        records.extend(d.get("records", []))
        offset = d.get("offset")
        if not offset:
            break

    deleted = 0
    for i in range(0, len(records), 10):
        batch = records[i:i+10]
        ids   = "&".join(f"records[]={r['id']}" for r in batch)
        url   = f"https://api.airtable.com/v0/{base_id}/{table_id}?{ids}"
        _at_req(url, method="DELETE", key=key)
        deleted += len(batch)
        time.sleep(0.25)
    print(f"  ✓ Deleted {deleted} records")


def push_to_airtable(key, base_id, table_id, jobs):
    """Push job records to Airtable."""
    created = []
    for job in jobs:
        fields = {
            "Job Title":     job["title"],
            "Company":       job["company"],
            "Score":         round(job["score"] * 100),
            "Location":      job["location"],
            "Salary":        job["salary"],
            "Platform":      job["platform"],
            "Match Reasons": job["match_reasons"],
            "Skill Gaps":    job["skill_gaps"],
            "Apply URL":     job["apply_url"],
            "Cover Letter":  job.get("cover_letter", ""),
            "Status":        "Ready to Apply",
        }
        data = json.dumps({"fields": fields}).encode()
        url  = f"https://api.airtable.com/v0/{base_id}/{table_id}"
        resp = _at_req(url, method="POST", data=data, key=key)
        created.append((job, resp["id"]))
        time.sleep(0.22)
    return created


# ── Search queries ────────────────────────────────────────────────────────────
QUERIES = [
    "IT Support Specialist remote",
    "IT Support Engineer remote",
    "Help Desk Technician remote",
    "Technical Support Engineer remote",
    "L2 Support Engineer remote",
    "L3 Support Engineer remote",
    "Cloud Support Engineer AWS remote",
    "Application Support Engineer remote",
    "DevOps Support Engineer remote",
    "IT Help Desk Engineer remote",
    "Desktop Support Engineer remote",
    "Systems Support Engineer Python remote",
]


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    sys.path.insert(0, SKILL_DIR)

    print(f"\n{'='*60}")
    print(f"  IT SUPPORT EA SEARCH — {TODAY}")
    print(f"  Target: {TARGET_TOTAL} jobs, ≥{TARGET_LI_EA} verified LinkedIn Easy Apply")
    print(f"  Freshness: ≤3 days | Min score: {MIN_SCORE:.0%}")
    print(f"{'='*60}")

    # ── 1. Search ─────────────────────────────────────────────────────────────
    print("\n  Searching JSearch (3-day window)...")
    seen_ids  = set()
    all_jobs  = []
    for query in QUERIES:
        print(f"    {query}...", end=" ", flush=True)
        try:
            results = jsearch(query, pages=1)
            new = 0
            for job in results:
                if not is_fresh(job):
                    continue
                jid = job.get("job_id") or job.get("job_apply_link", "")
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    all_jobs.append(job)
                    new += 1
            print(new)
        except Exception as e:
            print(f"error: {e}")
        time.sleep(1.2)

    print(f"\n  Total fresh jobs found: {len(all_jobs)}")

    # ── 2. Score ──────────────────────────────────────────────────────────────
    print(f"\n  Scoring {len(all_jobs)} jobs (min {MIN_SCORE:.0%})...")
    scored = []
    for i, job in enumerate(all_jobs):
        title   = job.get("job_title", "")
        company = job.get("employer_name", "")
        print(f"  [{i+1:3}/{len(all_jobs)}] {title[:40]} @ {company[:25]}...",
              end=" ", flush=True)

        score, scoring = score_job(job)
        print(f"{score:.2f}")

        if score >= MIN_SCORE:
            is_remote  = job.get("job_is_remote", False)
            location   = ("Remote" if is_remote
                          else (job.get("job_city") or "") + ", " + (job.get("job_state") or ""))
            sal_min    = job.get("job_min_salary") or 0
            sal_max    = job.get("job_max_salary") or 0
            apply_url  = job.get("job_apply_link", "")
            publisher  = job.get("job_publisher", "")
            is_li_url  = "linkedin.com" in apply_url.lower()
            is_direct  = job.get("job_apply_is_direct", True)
            # Candidate for LinkedIn EA: LinkedIn URL and not direct redirect
            li_ea_candidate = is_li_url and not is_direct

            scored.append({
                "job":          job,
                "score":        score,
                "scoring":      scoring,
                "title":        title,
                "company":      company,
                "location":     location,
                "salary":       (f"${int(sal_min):,} - ${int(sal_max):,}"
                                 if sal_max else "Not listed"),
                "apply_url":    apply_url,
                "platform":     publisher,
                "match_reasons": "; ".join(scoring.get("match_reasons", [])),
                "skill_gaps":   "; ".join(scoring.get("gaps", [])),
                "li_ea_candidate": li_ea_candidate,
                "li_ea_verified":  False,
            })
        time.sleep(0.5)

    scored.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  {len(scored)} jobs above {MIN_SCORE:.0%} threshold")

    if not scored:
        print("  No qualifying jobs found — try widening the search or lowering threshold.")
        return

    # ── 3. Verify LinkedIn Easy Apply on-page ─────────────────────────────────
    candidates_for_ea = [j for j in scored if j["li_ea_candidate"]]
    print(f"\n  Verifying LinkedIn Easy Apply for {len(candidates_for_ea)} candidates...")

    verified_ea = []
    for job in candidates_for_ea:
        print(f"    {job['company'][:35]} — {job['title'][:35]}...",
              end=" ", flush=True)
        ok = verify_linkedin_ea(job["apply_url"])
        job["li_ea_verified"] = ok
        status = "✓ Easy Apply" if ok else "✗ no Easy Apply"
        print(status)
        if ok:
            verified_ea.append(job)
        if len(verified_ea) >= TARGET_LI_EA + 2:  # collect a few extras
            break
        time.sleep(1)

    _close_playwright()

    print(f"\n  Verified LinkedIn EA: {len(verified_ea)}")

    # ── 4. Select final 5 ─────────────────────────────────────────────────────
    # Priority: verified EA first (up to TARGET_LI_EA), then highest-scored others
    ea_picks    = verified_ea[:TARGET_LI_EA]
    ea_urls     = {j["apply_url"] for j in ea_picks}
    other_picks = [j for j in scored if j["apply_url"] not in ea_urls]
    other_picks = other_picks[:TARGET_TOTAL - len(ea_picks)]
    final       = ea_picks + other_picks

    # If we couldn't get enough EA, just take top scored
    if len(final) < TARGET_TOTAL:
        remaining_urls = {j["apply_url"] for j in final}
        extra = [j for j in scored if j["apply_url"] not in remaining_urls]
        final += extra[:TARGET_TOTAL - len(final)]

    final = final[:TARGET_TOTAL]

    print(f"\n  Selected {len(final)} jobs:")
    for j in final:
        ea_tag = " [LinkedIn EA ✓]" if j["li_ea_verified"] else ""
        print(f"    {j['score']:.0%}  {j['title'][:45]} @ {j['company'][:25]}{ea_tag}")

    if not final:
        print("  Nothing to push.")
        return

    # ── 5. Tailor + cover letters ─────────────────────────────────────────────
    print(f"\n  Tailoring resume & cover letters...")
    try:
        from ai_tailoring import tailor_resume, write_cover_letter, format_base_resume
        _base_text = format_base_resume(p)
        _tailoring = True
    except Exception as e:
        print(f"  ⚠ ai_tailoring not available: {e}")
        _tailoring = False

    for entry in final:
        job   = entry["job"]
        desc  = (job.get("job_description") or "")[:2200]
        co, ti = entry["company"], entry["title"]
        print(f"    {co[:40]} — {ti[:30]}...", end=" ", flush=True)

        if _tailoring:
            try:
                tailored = tailor_resume(OPENAI_KEY, p, desc, co, ti)
                cl_res   = write_cover_letter(OPENAI_KEY, p, desc, co, ti,
                                              tailored_summary=tailored.get("summary", ""))
                entry["tailored_sections"] = tailored
                entry["cover_letter"]      = cl_res.get("cover_letter", "")
                print("✓ tailored")
            except Exception as e:
                entry["tailored_sections"] = {}
                entry["cover_letter"] = ""
                print(f"✗ {e}")
        else:
            entry["tailored_sections"] = {}
            entry["cover_letter"] = ""
            print("skipped")
        time.sleep(0.3)

    # ── 6. Generate PDFs ──────────────────────────────────────────────────────
    print(f"\n  Generating PDFs...")
    try:
        from pdf_generator import (
            generate_resume, generate_tailored_resume,
            generate_cover_letter, PDF_DIR
        )
        base_pdf = generate_resume()
        for entry in final:
            co, ti  = entry["company"], entry["title"]
            tailored = entry.get("tailored_sections", {})
            cl_text  = entry.get("cover_letter", "")

            if tailored and tailored.get("summary"):
                try:
                    entry["resume_pdf"] = generate_tailored_resume(tailored, co, ti)
                except Exception as e:
                    print(f"  ⚠ Tailored resume failed ({co}): {e}")
                    entry["resume_pdf"] = base_pdf
            else:
                entry["resume_pdf"] = base_pdf

            if cl_text and not cl_text.startswith("["):
                try:
                    entry["cover_pdf"] = generate_cover_letter(co, ti, cl_text)
                except Exception as e:
                    print(f"  ⚠ Cover PDF failed ({co}): {e}")
        print(f"  ✓ PDFs generated in {PDF_DIR}")
    except Exception as e:
        print(f"  ⚠ PDF generation error: {e}")

    # ── 7. Upload to Drive + patch Airtable ──────────────────────────────────
    print(f"\n  Uploading PDFs to Google Drive...")
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             os.path.join(SKILL_DIR, "drive_uploader.py"),
             "--date", TODAY],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            print("  ✓ Drive upload complete")
        else:
            print(f"  ⚠ Drive upload issue:\n{result.stderr[-300:]}")
    except Exception as e:
        print(f"  ⚠ Drive upload failed: {e}")

    # ── 8. Sync to Airtable ──────────────────────────────────────────────────
    print(f"\n  Pushing to Airtable...")
    at = _auth["airtable:default"]
    at_key, at_base_id, at_table_id = at["key"], at["base_id"], at["table_id"]

    clear_airtable(at_key, at_base_id, at_table_id)

    # Add EA tag to platform field for easy identification
    for entry in final:
        if entry.get("li_ea_verified"):
            entry["platform"] = entry["platform"] + " [Easy Apply]"

    created = push_to_airtable(at_key, at_base_id, at_table_id, final)
    print(f"  ✓ {len(created)} records pushed to Airtable")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  DONE — {len(final)} jobs pushed to Airtable")
    print(f"{'='*60}")
    ea_count = sum(1 for j in final if j.get("li_ea_verified"))
    print(f"  Verified LinkedIn Easy Apply: {ea_count}")
    print(f"  Other (direct apply):         {len(final) - ea_count}")
    print(f"\n  Jobs:")
    for j in final:
        ea_tag = " ← Easy Apply" if j.get("li_ea_verified") else ""
        print(f"    {j['score']:.0%}  {j['title'][:50]} @ {j['company']}{ea_tag}")
    print(f"\n  Airtable: https://airtable.com/{at_base_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
