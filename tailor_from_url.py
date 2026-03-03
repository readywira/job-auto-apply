#!/usr/bin/env python3
"""
Tailor Resume & Cover Letter from a Job URL
Fetches a job posting URL (with Jina AI Reader fallback for bot-protected sites),
extracts full job details, scores against profile, generates tailored resume + cover
letter PDFs, syncs all Airtable fields, and prints a short notification with the link.

Fetch strategy (tried in order):
  1. Direct HTTP fetch with browser headers  — fast, works for open sites
  2. Jina AI Reader (r.jina.ai)             — handles JS rendering + Cloudflare / Dice / Indeed
  3. Prompt user to paste job description   — last resort

Usage:
    python tailor_from_url.py "https://www.linkedin.com/jobs/view/..."
    python tailor_from_url.py "URL" --no-pdf
"""

import json, os, sys, re, urllib.request, urllib.parse, html
from datetime import datetime

SKILL_DIR    = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")
OUTPUT_BASE  = os.path.expanduser("~/job_applications")
TODAY        = datetime.now().strftime("%Y-%m-%d")
SKIP_PDF     = "--no-pdf" in sys.argv

sys.path.insert(0, SKILL_DIR)

# ── Validate args ─────────────────────────────────────────────────────────────
url_args = [a for a in sys.argv[1:] if a.startswith("http")]
if not url_args:
    print("Usage: python tailor_from_url.py \"https://job-url-here\"")
    sys.exit(1)
JOB_URL = url_args[0]

# ── Auth + profile ─────────────────────────────────────────────────────────────
with open(AUTH_FILE) as f:
    _auth = json.load(f)["profiles"]
OPENAI_KEY = _auth["openai:default"]["key"]

with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)["profile"]

# ── Build profile summary for GPT scoring (mirrors job_pipeline.py) ───────────
_p    = PROFILE
_per  = _p["personal"]
_exp  = _p["experience"]
_sk   = _p["skills"]
_pref = _p["preferences"]

PROFILE_SUMMARY = f"""
Name: {_per["full_name"]}
Current Title: {_exp["current_title"]}
Years of Experience: {_exp["years_total"]}
Location: {_per["location"]["city"]}, {_per["location"]["state"]}
Work Preference: {", ".join(_pref["work_arrangement"])}
Min Salary: ${_pref["salary_expectations"]["minimum"]:,}/yr
Skills: {", ".join(_sk.get("programming_languages", []) + _sk.get("frameworks", []) + _sk.get("tools", []))}
Certifications: {", ".join(_sk.get("certifications", []))}
LinkedIn: {_per.get("linkedin_url", "")}
GitHub: {_per.get("github_url", "")}
Work Authorization: Authorized, no sponsorship needed
""".strip()


# ── Platform detection from URL ───────────────────────────────────────────────
_PLATFORM_MAP = {
    "linkedin.com":        "linkedin",
    "indeed.com":          "indeed",
    "dice.com":            "dice",
    "glassdoor.com":       "glassdoor",
    "ziprecruiter.com":    "ziprecruiter",
    "wellfound.com":       "wellfound",
    "lever.co":            "lever",
    "greenhouse.io":       "greenhouse",
    "workday.com":         "workday",
    "smartrecruiters.com": "smartrecruiters",
    "icims.com":           "icims",
    "taleo.net":           "taleo",
    "jobvite.com":         "jobvite",
    "myworkdayjobs.com":   "workday",
}

def detect_platform(url):
    low = url.lower()
    for domain, name in _PLATFORM_MAP.items():
        if domain in low:
            return name
    return "url-tailor"


# ── Fetch helpers ──────────────────────────────────────────────────────────────
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _strip_html(raw):
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<style[^>]*>.*?</style>",  " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _looks_blocked(text):
    """
    Return True if the fetched text looks like a bot block, security page,
    or a JavaScript SPA shell with no actual content.
    """
    if len(text) < 500:
        return True
    lower = text.lower()
    # Explicit block / challenge pages
    block_signals = [
        "access denied", "403 forbidden", "cloudflare", "just a moment",
        "enable javascript", "checking your browser", "please verify",
        "captcha", "bot protection", "security check", "ddos protection",
        "you have been blocked",
    ]
    if any(s in lower for s in block_signals):
        return True
    # JS-rendered SPA shell: nav/header content only, no real job description.
    # Require at least 2 of these phrases — they appear in job descriptions
    # but never in nav menus or page shells.
    strong_signals = [
        "responsibilities", "requirements", "qualifications",
        "years of experience", "what you'll do", "what you bring",
        "minimum qualifications", "preferred qualifications",
        "about the role", "about this role", "job description",
        "we are looking for", "you will be", "you will have",
        "basic qualifications", "key responsibilities",
    ]
    matched = sum(1 for s in strong_signals if s in lower)
    return matched < 2

def fetch_direct(url, max_bytes=80_000):
    """Direct HTTP fetch with browser headers. Returns (text, ok)."""
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(max_bytes).decode("utf-8", errors="replace")
        text = _strip_html(raw)[:12_000]
        if _looks_blocked(text):
            return text, False
        return text, True
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}]", False
    except Exception as e:
        return f"[Error: {e}]", False

def fetch_via_jina(url, max_chars=12_000):
    """
    Fetch via Jina AI Reader (r.jina.ai).
    Handles JS rendering, Cloudflare, and most bot-protection layers.
    Free, no API key required.
    """
    jina_url = "https://r.jina.ai/" + url
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; JobPipeline/1.0)",
        "Accept": "text/plain",
        "X-No-Cache": "true",
    }
    req = urllib.request.Request(jina_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read(max_chars * 3).decode("utf-8", errors="replace")
        return text.strip()[:max_chars], True
    except Exception as e:
        return f"[Jina failed: {e}]", False

def fetch_page(url):
    """
    Multi-tier fetch. Returns (text, source) where source is
    'direct', 'jina', or 'failed'.
    """
    print("  Fetching (direct)...")
    text, ok = fetch_direct(url)
    if ok:
        return text, "direct"

    print(f"  Direct fetch blocked ({text[:50]}) — trying Jina AI Reader...")
    text, ok = fetch_via_jina(url)
    if ok:
        print("  ✓ Jina AI Reader succeeded")
        return text, "jina"

    print(f"  ⚠ Both fetch methods failed: {text[:80]}")
    return text, "failed"


# ── Extract full job details via GPT ─────────────────────────────────────────
def extract_job_details(page_text, url):
    """
    Use GPT-4o-mini to extract all structured fields from the raw page text.
    Returns dict with: job_title, company, location, is_remote, salary, description.
    """
    data = json.dumps({
        "model": "gpt-4o-mini",
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": (
                "Extract the job posting details from this page text.\n\n"
                f"SOURCE URL: {url}\n\n"
                f"PAGE TEXT (may be truncated):\n{page_text[:7000]}\n\n"
                "Return JSON with EXACTLY these keys:\n"
                '{\n'
                '  "job_title": "exact job title from posting",\n'
                '  "company": "employer / company name",\n'
                '  "location": "City, State or Remote",\n'
                '  "is_remote": true or false,\n'
                '  "salary": "e.g. $120,000 - $150,000/yr or empty string if not listed",\n'
                '  "description": "complete job description text including responsibilities, '
                'requirements, and qualifications — 500 to 2000 words"\n'
                '}\n\n'
                "If any field is unclear, make your best guess from context. "
                "For salary, look for ranges like $X - $Y, hourly rates, or annual figures. "
                "Return empty string if truly absent."
            )
        }],
        "max_tokens": 2500,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        content = json.loads(r.read())["choices"][0]["message"]["content"]
    return json.loads(content)


# ── Score job against profile ─────────────────────────────────────────────────
def score_job(job_title, company, location, salary, description):
    """
    Run the same GPT scoring prompt used in job_pipeline.py.
    Returns dict: score, match_reasons, gaps, salary_ok.
    """
    prompt = f"""You are a job match evaluator. Score this job for the candidate below.

CANDIDATE:
{PROFILE_SUMMARY}

JOB:
Title: {job_title}
Company: {company}
Location: {location}
Salary: {salary or "Not listed"}
Description: {description[:2500]}

Return ONLY valid JSON (no markdown):
{{
  "score": <0.0-1.0>,
  "match_reasons": ["reason1", "reason2", "reason3"],
  "gaps": ["gap1", "gap2"],
  "salary_ok": <true/false>
}}"""

    data = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"].strip()
    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


# ── Main ──────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  TAILOR FROM URL — {TODAY}")
print(f"  {JOB_URL[:70]}")
print(f"{'='*60}\n")

platform = detect_platform(JOB_URL)

# 1. Fetch
page_text, fetch_source = fetch_page(JOB_URL)

if fetch_source == "failed":
    print(f"\n{'='*60}")
    print("FETCH_FAILED")
    print(f"Could not fetch the page at: {JOB_URL}")
    print("Please paste the job description text directly in chat.")
    print(f"{'='*60}\n")
    sys.exit(0)

# 2. Extract job details
print("  Extracting job details...")
try:
    details     = extract_job_details(page_text, JOB_URL)
    job_title   = details.get("job_title", "Unknown Role")
    company     = details.get("company",   "Unknown Company")
    location    = details.get("location",  "Remote")
    salary      = details.get("salary",    "")
    description = details.get("description", page_text[:3000])
    if details.get("is_remote") and "remote" not in location.lower():
        location = f"{location} (Remote)"
except Exception as e:
    print(f"  ⚠ Extraction failed ({e}) — using raw text")
    job_title   = "Unknown Role"
    company     = "Unknown Company"
    location    = "Remote"
    salary      = ""
    description = page_text[:3000]

print(f"  Role:     {job_title}")
print(f"  Company:  {company}")
print(f"  Location: {location}")
print(f"  Salary:   {salary or 'Not listed'}")
print(f"  Source:   {fetch_source} / {platform}")

# 3. Score against profile
print("\n  Scoring against profile...")
scoring = {"score": 0, "match_reasons": [], "gaps": [], "salary_ok": False}
try:
    scoring = score_job(job_title, company, location, salary, description)
    score   = float(scoring.get("score", 0))
    print(f"  ✓ Match score: {score:.0%}")
    if scoring.get("match_reasons"):
        print(f"    Reasons: {'; '.join(scoring['match_reasons'][:2])}")
    if scoring.get("gaps"):
        print(f"    Gaps:    {'; '.join(scoring['gaps'][:2])}")
except Exception as e:
    print(f"  ⚠ Scoring failed ({e})")
    score = 0.0

# 4. Tailor resume
print("\n  Tailoring resume...")
from ai_tailoring import tailor_resume, write_cover_letter

try:
    tailored = tailor_resume(OPENAI_KEY, PROFILE, description, company, job_title)
except Exception as e:
    print(f"  ⚠ Resume tailoring failed: {e}")
    tailored = {}

# 5. Generate cover letter
print("  Writing cover letter...")
try:
    cl_result    = write_cover_letter(
        OPENAI_KEY, PROFILE, description, company, job_title,
        tailored_summary=tailored.get("summary", "")
    )
    cover_letter = cl_result.get("cover_letter", "")
    subject_line = cl_result.get("subject_line", "")
except Exception as e:
    print(f"  ⚠ Cover letter failed: {e}")
    cover_letter = ""
    subject_line = ""

# 6. Save text artifact
safe_co   = re.sub(r"[^\w\-]", "_", company).strip("_")[:40]
safe_ti   = re.sub(r"[^\w\-]", "_", job_title).strip("_")[:40]
cover_dir = os.path.join(OUTPUT_BASE, f"{TODAY}_cover_letters")
os.makedirs(cover_dir, exist_ok=True)
txt_path  = os.path.join(cover_dir, f"{safe_co}_{safe_ti}_url_tailor.txt")

with open(txt_path, "w") as f:
    f.write(f"Position: {job_title}\nCompany:  {company}\nSource:   {JOB_URL}\n")
    f.write(f"Score:    {score:.0%}\nSalary:   {salary or 'Not listed'}\n")
    if subject_line:
        f.write(f"Subject:  {subject_line}\n")
    f.write("=" * 60 + "\n\n")
    if scoring.get("match_reasons"):
        f.write("--- MATCH REASONS ---\n")
        for r in scoring["match_reasons"]:
            f.write(f"  ✓ {r}\n")
        f.write("\n")
    if scoring.get("gaps"):
        f.write("--- GAPS ---\n")
        for g in scoring["gaps"]:
            f.write(f"  △ {g}\n")
        f.write("\n")
    if tailored.get("summary"):
        f.write("--- TAILORED SUMMARY ---\n")
        f.write(tailored["summary"] + "\n\n")
    f.write("--- COVER LETTER ---\n")
    f.write(cover_letter)
    if tailored.get("keyword_map"):
        f.write("\n\n--- KEYWORD MAP ---\n")
        for req, loc in tailored["keyword_map"]:
            f.write(f"  {req:<35} → {loc}\n")

# 7. Generate PDFs
resume_pdf = ""
cover_pdf  = ""

if not SKIP_PDF:
    print("  Generating PDFs...")
    try:
        from pdf_generator import generate_tailored_resume, generate_cover_letter, generate_resume
        if tailored.get("summary"):
            resume_pdf = generate_tailored_resume(tailored, company, job_title)
        else:
            resume_pdf = generate_resume()
        if cover_letter:
            cover_pdf = generate_cover_letter(company, job_title, cover_letter)
        print(f"  ✓ Resume PDF: {resume_pdf}")
        print(f"  ✓ Cover PDF:  {cover_pdf}")
    except Exception as e:
        print(f"  ⚠ PDF generation failed: {e}")

# 8. Sync to Airtable — all columns
airtable_url = ""

try:
    from airtable_sync import load_auth, ensure_base, create_record, upload_pdfs

    at_key, at_base_id, at_table_id = load_auth()

    if at_base_id:
        at_base_id, at_table_id = ensure_base(at_key, at_base_id, at_table_id)

        job_data = {
            "title":         job_title,
            "company":       company,
            "location":      location,
            "salary":        salary,
            "platform":      platform,
            "apply_url":     JOB_URL,
            "cover_letter":  cover_letter,
            "score":         score,
            "match_reasons": "; ".join(scoring.get("match_reasons", [])),
            "skill_gaps":    "; ".join(scoring.get("gaps", [])),
            "resume_pdf":    resume_pdf,
            "cover_pdf":     cover_pdf,
        }

        print("  Syncing to Airtable...")
        record_id = create_record(at_key, at_base_id, job_data)

        if resume_pdf or cover_pdf:
            upload_pdfs(at_key, at_base_id, at_table_id, [(job_data, record_id)])

        airtable_url = f"https://airtable.com/{at_base_id}/{at_table_id}/{record_id}"
        print(f"  ✓ Airtable record: {airtable_url}")
    else:
        print("  ⚠ Airtable not configured — skipping sync")

except Exception as e:
    print(f"  ⚠ Airtable sync failed: {e}")

# 9. Print notification (agent relays this to user)
score_pct = f"{score:.0%}" if score else "unscored"

print(f"\n{'='*60}")
print("NOTIFICATION")
print(f"✅ Tailored: {job_title} at {company}")
print(f"📍 {location}" + (f"  💰 {salary}" if salary else ""))
print(f"📊 Match score: {score_pct}")
if subject_line:
    print(f"✉️  Subject: {subject_line}")
if airtable_url:
    print(f"🔗 Review & download PDFs: {airtable_url}")
else:
    def _to_win(p):
        return p.replace("/home/benji/", "\\\\wsl.localhost\\Ubuntu\\home\\benji\\").replace("/", "\\") if p else ""
    if resume_pdf: print(f"📄 Resume PDF: {_to_win(resume_pdf)}")
    if cover_pdf:  print(f"📄 Cover PDF:  {_to_win(cover_pdf)}")
print(f"{'='*60}\n")
