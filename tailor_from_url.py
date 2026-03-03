#!/usr/bin/env python3
"""
Tailor Resume & Cover Letter from a Job URL
Fetches a job posting URL, generates tailored resume + cover letter PDFs,
syncs to Airtable, and prints a short notification with the Airtable link.

Usage:
    python tailor_from_url.py "https://www.linkedin.com/jobs/view/..."
    python tailor_from_url.py "URL" --no-pdf

Output:
    - Tailored resume PDF + cover letter PDF saved to ~/job_applications/TODAY/pdfs/
    - Record created in Airtable (Pending Review)
    - Short NOTIFICATION block printed to stdout for the agent to relay
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

# ── Auth ──────────────────────────────────────────────────────────────────────
with open(AUTH_FILE) as f:
    _auth = json.load(f)["profiles"]
OPENAI_KEY = _auth["openai:default"]["key"]

with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)["profile"]

# ── Fetch job page ─────────────────────────────────────────────────────────────
def fetch_page(url, max_bytes=80_000):
    """Fetch URL, return raw text (HTML stripped to visible content)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(max_bytes).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  ⚠ HTTP {e.code} fetching page — will extract what we can from URL")
        return f"[Fetch failed: HTTP {e.code}. URL: {url}]"
    except Exception as e:
        print(f"  ⚠ Fetch error: {e}")
        return f"[Fetch failed: {e}. URL: {url}]"

    # Strip script/style tags
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<style[^>]*>.*?</style>",  " ", raw, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:12_000]  # cap for GPT context


# ── Extract job details via GPT ───────────────────────────────────────────────
def extract_job_details(page_text, url):
    """Use GPT to extract job title, company, and description from raw page text."""
    data = json.dumps({
        "model": "gpt-4o-mini",
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": (
                "Extract the job posting details from this page text.\n\n"
                f"SOURCE URL: {url}\n\n"
                f"PAGE TEXT (may be truncated):\n{page_text[:6000]}\n\n"
                "Return JSON with exactly these keys:\n"
                '{"job_title": "...", "company": "...", "location": "...", '
                '"is_remote": true/false, "description": "full job description text (500-2000 words)"}'
                "\n\nIf any field is unclear, make your best guess from context."
            )
        }],
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        content = json.loads(r.read())["choices"][0]["message"]["content"]
    return json.loads(content)


# ── Main ──────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  TAILOR FROM URL — {TODAY}")
print(f"  {JOB_URL[:70]}")
print(f"{'='*60}\n")

print("  Fetching job page...")
page_text = fetch_page(JOB_URL)

print("  Extracting job details...")
try:
    details = extract_job_details(page_text, JOB_URL)
    job_title   = details.get("job_title", "Unknown Role")
    company     = details.get("company", "Unknown Company")
    location    = details.get("location", "Remote")
    description = details.get("description", page_text[:3000])
except Exception as e:
    print(f"  ⚠ Extraction failed ({e}) — using raw page text")
    job_title   = "Unknown Role"
    company     = "Unknown Company"
    location    = "Remote"
    description = page_text[:3000]

print(f"  Role:    {job_title}")
print(f"  Company: {company}")
print(f"  Location:{location}")

# ── Tailor resume ─────────────────────────────────────────────────────────────
print("\n  Tailoring resume...")
from ai_tailoring import tailor_resume, write_cover_letter

try:
    tailored = tailor_resume(OPENAI_KEY, PROFILE, description, company, job_title)
except Exception as e:
    print(f"  ⚠ Resume tailoring failed: {e}")
    tailored = {}

# ── Generate cover letter ─────────────────────────────────────────────────────
print("  Writing cover letter...")
try:
    cl_result = write_cover_letter(
        OPENAI_KEY, PROFILE, description, company, job_title,
        tailored_summary=tailored.get("summary", "")
    )
    cover_letter = cl_result.get("cover_letter", "")
    subject_line = cl_result.get("subject_line", "")
except Exception as e:
    print(f"  ⚠ Cover letter failed: {e}")
    cover_letter = ""
    subject_line = ""

# ── Save text artifact ────────────────────────────────────────────────────────
safe_co = re.sub(r"[^\w\-]", "_", company).strip("_")[:40]
safe_ti = re.sub(r"[^\w\-]", "_", job_title).strip("_")[:40]
cover_dir = os.path.join(OUTPUT_BASE, f"{TODAY}_cover_letters")
os.makedirs(cover_dir, exist_ok=True)
txt_path = os.path.join(cover_dir, f"{safe_co}_{safe_ti}_url_tailor.txt")

with open(txt_path, "w") as f:
    f.write(f"Position: {job_title}\nCompany:  {company}\nSource:   {JOB_URL}\n")
    if subject_line:
        f.write(f"Subject:  {subject_line}\n")
    f.write("=" * 60 + "\n\n")
    if tailored.get("summary"):
        f.write("--- TAILORED SUMMARY ---\n")
        f.write(tailored["summary"] + "\n\n")
    f.write("--- COVER LETTER ---\n")
    f.write(cover_letter)
    if tailored.get("keyword_map"):
        f.write("\n\n--- KEYWORD MAP ---\n")
        for req, loc in tailored["keyword_map"]:
            f.write(f"  {req:<35} → {loc}\n")

# ── Generate PDFs ─────────────────────────────────────────────────────────────
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

# ── Sync to Airtable ──────────────────────────────────────────────────────────
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
            "apply_url":     JOB_URL,
            "cover_letter":  cover_letter,
            "score":         0,       # URL-tailored jobs start unscored
            "salary":        "",
            "platform":      "url-tailor",
            "match_reasons": "",
            "skill_gaps":    "",
            "resume_pdf":    resume_pdf,
            "cover_pdf":     cover_pdf,
        }

        print("  Syncing to Airtable...")
        record_id = create_record(at_key, at_base_id, job_data)

        # Write PDF paths to Notes field
        if resume_pdf or cover_pdf:
            upload_pdfs(at_key, at_base_id, at_table_id, [(job_data, record_id)])

        airtable_url = f"https://airtable.com/{at_base_id}/{at_table_id}/{record_id}"
        print(f"  ✓ Airtable record: {airtable_url}")
    else:
        print("  ⚠ Airtable not configured — skipping sync")

except Exception as e:
    print(f"  ⚠ Airtable sync failed: {e}")

# ── Print notification (agent relays this to user) ───────────────────────────
print(f"\n{'='*60}")
print("NOTIFICATION")
print(f"Tailored for: {job_title} at {company}")
if subject_line:
    print(f"Email subject: {subject_line}")
if airtable_url:
    print(f"Review & download PDFs: {airtable_url}")
else:
    # Fallback: Windows-accessible paths
    def to_win(p):
        return p.replace("/home/benji/", "\\\\wsl.localhost\\Ubuntu\\home\\benji\\").replace("/", "\\") if p else ""
    if resume_pdf:
        print(f"Resume PDF: {to_win(resume_pdf)}")
    if cover_pdf:
        print(f"Cover PDF:  {to_win(cover_pdf)}")
print(f"{'='*60}\n")
