#!/usr/bin/env python3
"""
RemoteOK Job Search — Startup-focused remote roles via RemoteOK's free JSON API.
Scores with DeepSeek (OpenAI fallback), tailors top matches, syncs to Airtable.

Usage:
    python3 remoteok_search.py              # search + score + tailor + Airtable
    python3 remoteok_search.py --limit 5   # top N matches (default 5)
    python3 remoteok_search.py --no-tailor # skip AI tailoring (faster)
    python3 remoteok_search.py --no-pdf    # skip PDF generation

Why RemoteOK instead of Wellfound:
    Wellfound blocks all scrapers (including Jina) with DataDome.
    RemoteOK has no CAPTCHA, returns a free JSON API, and lists many of the same
    startup roles that redirect to Greenhouse / Lever / Ashby for apply.
"""

import json, os, sys, time, re, urllib.request, urllib.error
from datetime import datetime

SKILL_DIR    = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")
OUTPUT_DIR   = os.path.expanduser("~/job_applications")

sys.path.insert(0, SKILL_DIR)

# ── Auth ──────────────────────────────────────────────────────────────────────
with open(AUTH_FILE) as f:
    _auth = json.load(f)["profiles"]

OPENAI_KEY   = _auth["openai:default"]["key"]
DEEPSEEK_KEY = _auth.get("deepseek:default", {}).get("key", "")

# ── Profile ───────────────────────────────────────────────────────────────────
with open(PROFILE_PATH) as f:
    _raw = json.load(f)
PROFILE  = _raw["profile"]
p        = PROFILE
NAME     = p["personal"]["full_name"]
EMAIL    = p["personal"]["email"]
PHONE    = p["personal"]["phone"]
LOCATION = f"{p['personal']['location']['city']}, {p['personal']['location']['state']}"
TITLE    = p["experience"]["current_title"]
YOE      = p["experience"]["years_total"]
SAL_MIN  = p["preferences"]["salary_expectations"]["minimum"]
SKILLS   = ", ".join(
    p["skills"].get("programming_languages", []) +
    p["skills"].get("frameworks", []) +
    p["skills"].get("tools", [])
)
CERTS    = ", ".join(p["skills"].get("certifications", []))

PROFILE_SUMMARY = f"""
Name: {NAME}
Current Title: {TITLE}
Years of Experience: {YOE}
Location: {LOCATION} (open to remote)
Min Salary: ${SAL_MIN:,}/yr
Skills: {SKILLS}
Certifications: {CERTS}
Work Authorization: Authorized in the US — no sponsorship needed
""".strip()

# ── CLI flags ─────────────────────────────────────────────────────────────────
SKIP_TAILOR = "--no-tailor" in sys.argv
SKIP_PDF    = "--no-pdf"    in sys.argv
TOP_N = 5
if "--limit" in sys.argv:
    TOP_N = int(sys.argv[sys.argv.index("--limit") + 1])

TODAY = datetime.now().strftime("%Y-%m-%d")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── LLM helper (OpenAI → DeepSeek fallback) ───────────────────────────────────
def _llm(url, key, model, prompt, max_tokens):
    data = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def llm(prompt, max_tokens=600):
    """Try OpenAI gpt-4o-mini, fall back to DeepSeek on quota/rate-limit errors."""
    try:
        return _llm("https://api.openai.com/v1/chat/completions",
                    OPENAI_KEY, "gpt-4o-mini", prompt, max_tokens)
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 503) and DEEPSEEK_KEY:
            return _llm("https://api.deepseek.com/v1/chat/completions",
                        DEEPSEEK_KEY, "deepseek-chat", prompt, max_tokens)
        raise


# ── RemoteOK fetch ─────────────────────────────────────────────────────────────
TARGET_ROLES = [
    "support engineer", "technical support", "it support", "helpdesk", "help desk",
    "devops", "cloud engineer", "sre", "site reliability",
    "full stack", "fullstack", "full-stack",
    "frontend", "front-end", "web developer",
]

# Exclude EMEA-only or Asia-only postings (Benjamin is US-based)
LOCATION_EXCLUDES = ["emea", "europe only", "uk only", "eu only", "asia only", "apac only"]


def fetch_remoteok():
    print("  Fetching RemoteOK jobs...")
    req = urllib.request.Request(
        "https://remoteok.com/api",
        headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        jobs = json.loads(r.read())

    # Skip first item (legal notice dict with no 'position')
    jobs = [j for j in jobs if j.get("position")]
    print(f"  Total RemoteOK listings: {len(jobs)}")

    matches = []
    for j in jobs:
        text = (j.get("position", "") + " " + str(j.get("tags", ""))).lower()
        if not any(k in text for k in TARGET_ROLES):
            continue
        loc = j.get("location", "").lower()
        if any(ex in loc for ex in LOCATION_EXCLUDES):
            continue
        matches.append(j)

    print(f"  Profile-matched listings: {len(matches)}")
    return matches


# ── Score ─────────────────────────────────────────────────────────────────────
def score_job(job):
    title   = job.get("position", "")
    company = job.get("company", "")
    desc    = re.sub(r"<[^>]+>", " ", job.get("description", ""))[:2000]
    loc     = job.get("location", "Remote")

    prompt = f"""Score this job for the candidate. Return ONLY valid JSON, no markdown.

CANDIDATE:
{PROFILE_SUMMARY}

JOB:
Title: {title}
Company: {company}
Location: {loc}
Description: {desc}

Return:
{{"score": <0.0-1.0>, "match_reasons": ["reason1"], "gaps": ["gap1"], "salary_ok": true}}"""

    try:
        raw = llm(prompt, max_tokens=200)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        return float(parsed.get("score", 0)), parsed
    except Exception as e:
        return 0.0, {"score": 0, "match_reasons": [], "gaps": [str(e)], "salary_ok": False}


# ── Tailor ─────────────────────────────────────────────────────────────────────
def tailor_job(job, desc):
    title   = job.get("position", "")
    company = job.get("company", "")

    try:
        from ai_tailoring import tailor_resume, write_cover_letter as ai_cover, format_base_resume
        tailored = tailor_resume(OPENAI_KEY, PROFILE, desc, company, title)
        cl_result = ai_cover(OPENAI_KEY, PROFILE, desc, company, title,
                             tailored_summary=tailored.get("summary", ""))
        return tailored, cl_result.get("cover_letter", ""), cl_result.get("subject_line", "")
    except Exception as e:
        # Plain cover letter fallback
        prompt = (f"Write a 3-paragraph professional cover letter for {NAME} applying to "
                  f"{title} at {company}. End with contact: {EMAIL} | {PHONE}. "
                  f"No placeholder brackets.\n\nJob:\n{desc[:1500]}")
        try:
            cl = llm(prompt, max_tokens=500)
        except Exception:
            cl = ""
        return {}, cl, f"{title} — {NAME}"


# ── Airtable sync ──────────────────────────────────────────────────────────────
def sync_to_airtable(jobs_data):
    try:
        from airtable_sync import (
            load_auth as at_load_auth, ensure_base,
            fetch_existing_urls, batch_create_records, upload_pdfs,
        )
        at_key, at_base_id, at_table_id = at_load_auth()
        at_base_id, at_table_id = ensure_base(at_key, at_base_id, at_table_id)
        existing = fetch_existing_urls(at_key, at_base_id, at_table_id)
        created  = batch_create_records(at_key, at_base_id, jobs_data, existing)
        pdfs     = [(j, rid) for j, rid in created if j.get("resume_pdf") or j.get("cover_pdf")]
        if pdfs:
            upload_pdfs(at_key, at_base_id, at_table_id, pdfs)
        print(f"  ✓ Airtable: {len(created)} new records pushed")
    except Exception as e:
        print(f"  ⚠ Airtable sync failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  REMOTEOK SEARCH — {TODAY}")
    print(f"  Top matches: {TOP_N} | Tailor: {not SKIP_TAILOR}")
    print(f"{'='*60}\n")

    listings = fetch_remoteok()
    if not listings:
        print("  No matching listings found.")
        return

    # ── Score ─────────────────────────────────────────────────────────────────
    print(f"\n  Scoring {len(listings)} listings...")
    scored = []
    for i, job in enumerate(listings, 1):
        title   = job.get("position", "?")
        company = job.get("company", "?")
        print(f"  [{i}/{len(listings)}] {title} @ {company}...", end=" ", flush=True)
        score, scoring = score_job(job)
        print(f"score={score:.2f}")
        if score >= 0.70:
            scored.append({"job": job, "score": score, "scoring": scoring})
        time.sleep(0.3)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_N]
    print(f"\n  Matched {len(scored)} above 0.70 threshold → taking top {len(top)}")

    if not top:
        print("  No matches above threshold.")
        return

    # ── Tailor + PDF + Drive ──────────────────────────────────────────────────
    at_jobs = []

    # Load PDF + Drive helpers (optional — skip if libs not installed)
    _pdf_gen    = None
    _drive_up   = None
    if not SKIP_PDF:
        try:
            import pdf_generator as _pdf_gen
            import drive_uploader as _drive_up
        except Exception as _e:
            print(f"  ⚠ PDF/Drive libs not available ({_e}) — skipping PDFs")

    for entry in top:
        job     = entry["job"]
        title   = job.get("position", "")
        company = job.get("company", "")
        score   = entry["score"]
        desc    = re.sub(r"<[^>]+>", " ", job.get("description", ""))
        rok_url = job.get("url", job.get("apply_url", ""))

        print(f"\n  Tailoring: {title} @ {company} ({score:.0%})...")

        cover_letter      = ""
        subject_line      = ""
        tailored_sections = {}
        resume_pdf        = ""
        cover_pdf         = ""
        resume_url        = ""
        cover_url         = ""

        if not SKIP_TAILOR:
            tailored_sections, cover_letter, subject_line = tailor_job(job, desc)
            print(f"    ✓ tailored")

        # Generate PDFs
        if _pdf_gen and not SKIP_PDF:
            try:
                if tailored_sections and tailored_sections.get("summary"):
                    resume_pdf = _pdf_gen.generate_tailored_resume(tailored_sections, company, title)
                else:
                    resume_pdf = _pdf_gen.generate_resume()
                if cover_letter and not cover_letter.startswith("["):
                    cover_pdf = _pdf_gen.generate_cover_letter(company, title, cover_letter)
                print(f"    ✓ PDFs generated")
            except Exception as _e:
                print(f"    ⚠ PDF failed: {_e}")

        # Upload to Google Drive
        if _drive_up and (resume_pdf or cover_pdf):
            try:
                urls = _drive_up.upload_pdfs_for_job(company, title, resume_pdf, cover_pdf)
                resume_url = urls.get("resume_url", "")
                cover_url  = urls.get("cover_url", "")
                print(f"    ✓ Drive uploaded")
            except Exception as _e:
                print(f"    ⚠ Drive upload failed: {_e}")

        # Tiered status: 85%+ → Ready to Apply directly, 70–84% → Pending Review
        at_status = "Ready to Apply" if score >= 0.85 else "Pending Review"

        at_jobs.append({
            "title":         title,
            "company":       company,
            "score":         score,
            "location":      job.get("location", "Remote"),
            "salary":        (f"${job['salary_min']:,}–${job['salary_max']:,}"
                              if job.get("salary_max") else "Not listed"),
            "platform":      "remoteok",
            "match_reasons": "; ".join(entry["scoring"].get("match_reasons", [])),
            "skill_gaps":    "; ".join(entry["scoring"].get("gaps", [])),
            "apply_url":     rok_url,
            "cover_letter":  cover_letter,
            "resume_pdf":    resume_pdf,
            "cover_pdf":     cover_pdf,
            "resume_url":    resume_url,
            "cover_url":     cover_url,
            "status":        at_status,
        })

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  TOP {len(top)} MATCHES")
    print(f"{'='*60}")
    for e in at_jobs:
        status_tag = "🟢 Ready" if e["status"] == "Ready to Apply" else "🟡 Review"
        print(f"  {e['score']:.0%}  {e['title']} @ {e['company']}  {status_tag}")
        print(f"        {e['apply_url']}")

    # ── Airtable ──────────────────────────────────────────────────────────────
    print(f"\n  Syncing to Airtable...")
    sync_to_airtable(at_jobs)

    print(f"\n{'='*60}")
    print(f"  Done. Run gobii_apply.py to auto-apply to Ready jobs.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
