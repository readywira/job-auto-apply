#!/usr/bin/env python3
"""
Benjamin's Job Search Pipeline
- Searches JSearch API (LinkedIn, Indeed, Glassdoor, ZipRecruiter)
- Scores each job against profile using GPT-4o-mini
- Generates tailored cover letters for top matches
- Saves results to CSV + JSON + individual cover letter files

Flags:
    0.75              -- minimum match score (default 0.70)
    --no-pdf          -- skip PDF generation
    --no-airtable     -- skip Airtable sync
    --boards linkedin,indeed,glassdoor,ziprecruiter,wellfound,all
                      -- filter by job board (default: all)

Examples:
    python job_pipeline.py
    python job_pipeline.py 0.80 --boards linkedin,indeed
    python job_pipeline.py --no-pdf --boards glassdoor
"""

import json, urllib.request, urllib.parse, time, csv, os, sys
from datetime import datetime

# ── Pipeline integration flags ────────────────────────────────────────────────
SKIP_PDF      = "--no-pdf"      in sys.argv
SKIP_AIRTABLE = "--no-airtable" in sys.argv

# ── Board filter ─────────────────────────────────────────────────────────────
_boards_arg = next((a for a in sys.argv[1:] if a.startswith("--boards=")), None)
if not _boards_arg:
    _boards_flag = next((i for i, a in enumerate(sys.argv) if a == "--boards"), None)
    _boards_arg  = sys.argv[_boards_flag + 1] if _boards_flag and _boards_flag + 1 < len(sys.argv) else "all"
else:
    _boards_arg = _boards_arg.split("=", 1)[1]

BOARD_FILTER = set()
if _boards_arg.lower() != "all":
    # Map friendly names to JSearch publisher substrings
    _board_map = {
        "linkedin":     "linkedin",
        "indeed":       "indeed",
        "glassdoor":    "glassdoor",
        "ziprecruiter": "ziprecruiter",
        "wellfound":    "wellfound",
    }
    for b in _boards_arg.lower().split(","):
        b = b.strip()
        if b in _board_map:
            BOARD_FILTER.add(_board_map[b])

# ── Config ───────────────────────────────────────────────────────────────────
PROFILE_PATH   = os.path.expanduser("~/job_profile.json")
AUTH_FILE      = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
OUTPUT_DIR     = os.path.expanduser("~/job_applications")

_auth          = json.load(open(AUTH_FILE))["profiles"]
OPENAI_KEY     = _auth["openai:default"]["key"]
RAPIDAPI_KEY   = _auth["rapidapi:default"]["key"]
PROFILE        = json.load(open(PROFILE_PATH))

MIN_SCORE      = float(next((a for a in sys.argv[1:] if a.replace('.','',1).isdigit()), "0.70"))
MAX_PER_TITLE  = 10   # JSearch results per job title query

os.makedirs(OUTPUT_DIR, exist_ok=True)
TODAY          = datetime.now().strftime("%Y-%m-%d")
COVER_DIR      = os.path.join(OUTPUT_DIR, f"{TODAY}_cover_letters")
os.makedirs(COVER_DIR, exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────
def gpt(prompt, max_tokens=600):
    data = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

def jsearch(query, pages=1):
    q = urllib.parse.quote(query)
    url = (f"https://jsearch.p.rapidapi.com/search"
           f"?query={q}&page=1&num_pages={pages}&country=us&date_posted=month")
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
        "x-rapidapi-key": RAPIDAPI_KEY
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("data", [])

# ── Profile summary for GPT prompts ─────────────────────────────────────────
p        = PROFILE["profile"]
NAME     = p["personal"]["full_name"]
EMAIL    = p["personal"]["email"]
PHONE    = p["personal"]["phone"]
LOCATION = f"{p['personal']['location']['city']}, {p['personal']['location']['state']}"
LINKEDIN = p["personal"]["linkedin_url"]
GITHUB   = p["personal"]["github_url"]
TITLE    = p["experience"]["current_title"]
YOE      = p["experience"]["years_total"]
SKILLS   = (
    ", ".join(p["skills"]["programming_languages"]) + ", " +
    ", ".join(p["skills"]["frameworks"]) + ", " +
    ", ".join(p["skills"]["tools"])
)
CERTS    = ", ".join(p["skills"].get("certifications", []))
SAL_MIN  = p["preferences"]["salary_expectations"]["minimum"]
WORK_ARR = ", ".join(p["preferences"]["work_arrangement"])

PROFILE_SUMMARY = f"""
Name: {NAME}
Current Title: {TITLE}
Years of Experience: {YOE}
Location: {LOCATION}
Work Preference: {WORK_ARR}
Min Salary: ${SAL_MIN:,}/yr
Skills: {SKILLS}
Certifications: {CERTS}
LinkedIn: {LINKEDIN}
GitHub: {GITHUB}
Work Authorization: Authorized, no sponsorship needed
""".strip()

# ── Step 1: Search jobs ──────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  JOB SEARCH PIPELINE — {TODAY}")
if BOARD_FILTER:
    print(f"  Boards: {', '.join(sorted(BOARD_FILTER))}")
else:
    print(f"  Boards: all")
print(f"{'='*60}")

job_titles    = PROFILE["search_criteria"]["job_titles"]
locations     = [LOCATION, "remote"]
seen_ids      = set()
all_jobs      = []

for title in job_titles:
    for loc in locations:
        query = f"{title} {loc}"
        print(f"\n  Searching: {query}...")
        try:
            results = jsearch(query, pages=1)
            new = 0
            for job in results:
                # Board filter: skip if publisher doesn't match selected boards
                if BOARD_FILTER:
                    publisher = job.get("job_publisher", "").lower()
                    if not any(b in publisher for b in BOARD_FILTER):
                        continue
                jid = job.get("job_id", job.get("job_apply_link", ""))
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    all_jobs.append(job)
                    new += 1
                    if new >= MAX_PER_TITLE:
                        break
            print(f"    → {new} new listings")
        except Exception as e:
            print(f"    → Error: {e}")
        time.sleep(1.2)  # respect rate limits

print(f"\n  Total unique jobs found: {len(all_jobs)}")

# ── Step 2: Score & filter ───────────────────────────────────────────────────
print(f"\n  Scoring jobs (min score: {MIN_SCORE})...")

scored_jobs = []

for i, job in enumerate(all_jobs):
    title       = job.get("job_title", "")
    company     = job.get("employer_name", "")
    location    = (job.get("job_city") or "") + ", " + (job.get("job_state") or "")
    is_remote   = job.get("job_is_remote", False)
    salary_min  = job.get("job_min_salary") or 0
    salary_max  = job.get("job_max_salary") or 0
    description = (job.get("job_description") or "")[:2500]
    apply_url   = job.get("job_apply_link", "")
    platform    = job.get("job_publisher", "")

    print(f"  [{i+1}/{len(all_jobs)}] {title} @ {company}...", end=" ", flush=True)

    score_prompt = f"""You are a job match evaluator. Score this job for the candidate below.

CANDIDATE:
{PROFILE_SUMMARY}

JOB:
Title: {title}
Company: {company}
Location: {"Remote" if is_remote else location}
Salary: {"${:,} - ${:,}".format(int(salary_min), int(salary_max)) if salary_max else "Not listed"}
Description: {description}

Return ONLY valid JSON (no markdown):
{{
  "score": <0.0-1.0>,
  "match_reasons": ["reason1", "reason2"],
  "gaps": ["gap1"],
  "salary_ok": <true/false>
}}"""

    try:
        raw     = gpt(score_prompt, max_tokens=200)
        # Strip markdown code blocks if present
        raw     = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        scoring = json.loads(raw)
        score   = float(scoring.get("score", 0))
    except Exception as e:
        score   = 0.0
        scoring = {"score": 0, "match_reasons": [], "gaps": [str(e)], "salary_ok": False}

    print(f"score={score:.2f}")

    if score >= MIN_SCORE:
        scored_jobs.append({
            "job":     job,
            "scoring": scoring,
            "score":   score,
            "title":   title,
            "company": company,
            "location": "Remote" if is_remote else location,
            "salary":  f"${int(salary_min):,} - ${int(salary_max):,}" if salary_max else "Not listed",
            "apply_url": apply_url,
            "platform":  platform,
        })

    time.sleep(0.5)

scored_jobs.sort(key=lambda x: x["score"], reverse=True)
print(f"\n  Matched {len(scored_jobs)} jobs above {MIN_SCORE} threshold")

# ── Step 3: Tailor resume + generate cover letters (v2 AI engine) ─────────────
print(f"\n  Tailoring resume & cover letters (v2 AI engine)...")

try:
    _skill_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _skill_dir)
    from ai_tailoring import tailor_resume, write_cover_letter as ai_cover_letter
    from ai_tailoring import format_base_resume
    _profile_data = PROFILE["profile"]
    _base_resume_text = format_base_resume(_profile_data)
    _TAILORING_AVAILABLE = True
except Exception as _te:
    print(f"  ⚠ ai_tailoring not available ({_te}) — using v1 prompts")
    _TAILORING_AVAILABLE = False

for entry in scored_jobs:
    job         = entry["job"]
    title       = entry["title"]
    company     = entry["company"]
    description = (job.get("job_description") or "")[:2200]

    print(f"    {company[:40]} — {title[:30]}...", end=" ", flush=True)

    if _TAILORING_AVAILABLE:
        # ── v2: tailor resume + structured cover letter ────────────────────────
        try:
            tailored = tailor_resume(OPENAI_KEY, _profile_data, description, company, title)
            entry["tailored_sections"] = tailored

            cl_result = ai_cover_letter(
                OPENAI_KEY, _profile_data, description, company, title,
                tailored_summary=tailored.get("summary", "")
            )
            entry["cover_letter"]   = cl_result.get("cover_letter", "")
            entry["subject_line"]   = cl_result.get("subject_line", "")
            entry["swap_tokens"]    = cl_result.get("swap_tokens", [])
            entry["keyword_map"]    = tailored.get("keyword_map", [])

            # Print any metric questions (up to 3)
            questions = tailored.get("questions", [])[:3]
            if questions:
                entry["metric_questions"] = questions

            print("✓ tailored")
        except Exception as e:
            entry["tailored_sections"] = {}
            entry["cover_letter"] = f"[Tailoring failed: {e}]"
            print(f"✗ {e}")
    else:
        # ── v1 fallback: plain cover letter ───────────────────────────────────
        cl_prompt = (
            f"Write a concise professional cover letter for {NAME} applying to {title} "
            f"at {company}. Use 3 paragraphs. End with: {NAME} | {EMAIL} | {PHONE} | {LINKEDIN}. "
            f"Do not use placeholder brackets.\n\nJob description:\n{description}"
        )
        try:
            entry["cover_letter"] = gpt(cl_prompt, max_tokens=500)
        except Exception as e:
            entry["cover_letter"] = f"[Cover letter failed: {e}]"
        entry["tailored_sections"] = {}
        print("✓ v1")

    # Save text artifact for review
    safe_co    = "".join(c if c.isalnum() or c in " -_" else "" for c in company).strip()
    safe_ti    = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
    filename   = f"{safe_co}_{safe_ti}.txt".replace(" ", "_")
    filepath   = os.path.join(COVER_DIR, filename)
    with open(filepath, "w") as f:
        f.write(f"Position: {title}\nCompany:  {company}\nApply:    {entry['apply_url']}\n")
        f.write(f"Score:    {entry['score']:.0%}\nSalary:   {entry['salary']}\n")
        if entry.get("subject_line"):
            f.write(f"Subject:  {entry['subject_line']}\n")
        f.write("="*60 + "\n\n")
        if entry.get("tailored_sections", {}).get("summary"):
            f.write("--- TAILORED SUMMARY ---\n")
            f.write(entry["tailored_sections"]["summary"] + "\n\n")
        f.write("--- COVER LETTER ---\n")
        f.write(entry["cover_letter"])
        if entry.get("keyword_map"):
            f.write("\n\n--- KEYWORD MAP ---\n")
            for req, loc in entry["keyword_map"]:
                f.write(f"  {req:<35} → {loc}\n")
        if entry.get("metric_questions"):
            f.write("\n\n--- METRIC QUESTIONS ---\n")
            for q in entry["metric_questions"]:
                f.write(f"  ? {q}\n")

    time.sleep(0.3)

# ── Step 4: Save CSV ─────────────────────────────────────────────────────────
csv_path  = os.path.join(OUTPUT_DIR, f"{TODAY}_matches.csv")
json_path = os.path.join(OUTPUT_DIR, f"{TODAY}_matches.json")

with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "score", "title", "company", "location", "salary",
        "platform", "match_reasons", "gaps", "apply_url"
    ])
    writer.writeheader()
    for entry in scored_jobs:
        writer.writerow({
            "score":         f"{entry['score']:.0%}",
            "title":         entry["title"],
            "company":       entry["company"],
            "location":      entry["location"],
            "salary":        entry["salary"],
            "platform":      entry["platform"],
            "match_reasons": "; ".join(entry["scoring"].get("match_reasons", [])),
            "gaps":          "; ".join(entry["scoring"].get("gaps", [])),
            "apply_url":     entry["apply_url"],
        })

with open(json_path, "w") as f:
    json.dump(scored_jobs, f, indent=2, default=str)

# ── Step 5: Generate PDFs ─────────────────────────────────────────────────────
pdf_dir = os.path.join(OUTPUT_DIR, f"{TODAY}", "pdfs")
resume_pdf_path = None

if not SKIP_PDF:
    print(f"\n  Generating PDFs (tailored per job)...")
    try:
        _skill_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _skill_dir)
        from pdf_generator import (
            generate_resume, generate_tailored_resume,
            generate_cover_letter, PDF_DIR
        )
        # Always generate the generic resume as fallback / for non-tailored runs
        resume_pdf_path = generate_resume()

        for entry in scored_jobs:
            cl_text  = entry.get("cover_letter", "")
            tailored = entry.get("tailored_sections", {})
            co       = entry["company"]
            ti       = entry["title"]

            # Per-job tailored resume PDF (if we have tailored sections)
            if tailored and tailored.get("summary"):
                try:
                    entry["resume_pdf"] = generate_tailored_resume(tailored, co, ti)
                except Exception as e:
                    print(f"  ⚠ Tailored resume failed for {co}: {e}")
                    entry["resume_pdf"] = resume_pdf_path
            else:
                entry["resume_pdf"] = resume_pdf_path

            # Cover letter PDF
            if cl_text and not cl_text.startswith("["):
                try:
                    entry["cover_pdf"] = generate_cover_letter(co, ti, cl_text)
                except Exception as e:
                    print(f"  ⚠ Cover PDF failed for {co}: {e}")

    except Exception as e:
        print(f"  ⚠ PDF generation failed: {e}")
        print(f"    (Install: pip install reportlab Pillow)")
else:
    print(f"\n  Skipping PDF generation (--no-pdf)")

# ── Step 6: Sync to Airtable ──────────────────────────────────────────────────
if not SKIP_AIRTABLE:
    print(f"\n  Syncing to Airtable...")
    try:
        _skill_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _skill_dir)
        from airtable_sync import (
            load_auth as at_load_auth,
            ensure_base,
            fetch_existing_urls,
            batch_create_records,
            upload_pdfs,
        )
        # Normalize entries for airtable_sync
        at_jobs = []
        for entry in scored_jobs:
            at_jobs.append({
                "title":         entry.get("title", ""),
                "company":       entry.get("company", ""),
                "score":         entry.get("score", 0),
                "location":      entry.get("location", ""),
                "salary":        entry.get("salary", ""),
                "platform":      entry.get("platform", ""),
                "match_reasons": "; ".join(entry.get("scoring", {}).get("match_reasons", [])),
                "skill_gaps":    "; ".join(entry.get("scoring", {}).get("gaps", [])),
                "apply_url":     entry.get("apply_url", ""),
                "cover_letter":  entry.get("cover_letter", ""),
                "resume_pdf":    entry.get("resume_pdf", ""),
                "cover_pdf":     entry.get("cover_pdf", ""),
            })

        at_key, at_base_id, at_table_id = at_load_auth()
        at_base_id, at_table_id = ensure_base(at_key, at_base_id, at_table_id)
        existing_urls   = fetch_existing_urls(at_key, at_base_id, at_table_id)
        created_pairs   = batch_create_records(at_key, at_base_id, at_jobs, existing_urls)
        pairs_with_pdfs = [(j, rid) for j, rid in created_pairs
                           if j.get("resume_pdf") or j.get("cover_pdf")]
        if pairs_with_pdfs:
            upload_pdfs(at_key, at_base_id, at_table_id, pairs_with_pdfs)
        print(f"  ✓ Airtable: {len(created_pairs)} new records pushed")
    except SystemExit:
        print(f"  ⚠ Airtable sync skipped — add API key to auth-profiles.json first")
    except Exception as e:
        print(f"  ⚠ Airtable sync failed: {e}")
else:
    print(f"\n  Skipping Airtable sync (--no-airtable)")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RESULTS SUMMARY")
print(f"{'='*60}")
print(f"  Jobs searched:  {len(all_jobs)}")
print(f"  Good matches:   {len(scored_jobs)}")
print(f"  CSV:            {csv_path}")
print(f"  Cover letters:  {COVER_DIR}/")
if resume_pdf_path:
    print(f"  Resume PDF:     {resume_pdf_path}")
print(f"\n  TOP MATCHES:")
for e in scored_jobs[:10]:
    print(f"    {e['score']:.0%}  {e['title']} @ {e['company']}  [{e['location']}]  {e['salary']}")
print(f"{'='*60}\n")
