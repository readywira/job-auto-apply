# job-auto-apply

An end-to-end job search and application pipeline powered by GPT-4o-mini, JSearch API, Playwright, and Airtable — with native AI agent integration via WhatsApp and Telegram.

---

## Features

### Multi-Platform Job Search
- Searches LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Wellfound simultaneously via the JSearch (RapidAPI) aggregator
- Filters by job board, recency (today / 3 days / this week / this month), and experience range
- Deduplicates across queries so each listing is scored only once

### AI-Powered Resume Tailoring
- GPT-4o-mini rewrites every resume bullet for each job using an **action + artifact + tools + impact** structure
- Generates a tailored 2-sentence professional summary per job
- Produces a keyword map showing which JD requirements are covered and where
- Flags missing metrics with targeted questions (e.g. "What is the latency improvement for X?")
- ATS-aligned: mirrors JD language without keyword stuffing

### Structured Cover Letter Generation
- 250–350 word cover letters in a strict 3-part structure: intro → 3 bullet highlights → CTA
- Each bullet references a specific JD requirement and maps it to a proof point
- Generates an email subject line and 5 swap tokens for easy iteration
- No clichés, no placeholder brackets, names real company and role throughout

### URL-Based Tailoring
- Send any job posting URL to the pipeline — it fetches, extracts, and tailors instantly
- Works with LinkedIn, Indeed, Greenhouse, Lever, Workday, and most ATS pages
- Automatically creates an Airtable record with status "Pending Review" and PDF paths

### PDF Generation
- Produces a tailored resume PDF and cover letter PDF per application using `reportlab`
- Per-job PDFs stored in `~/job_applications/YYYY-MM-DD/pdfs/`
- Falls back to the generic resume PDF if tailoring is unavailable

### Airtable Tracking
- All jobs (from pipeline or URL tailor) are synced to an Airtable "Job Applications" base
- Full deduplication — already-synced jobs are skipped
- Status workflow: `Pending Review` → `Ready to Apply` → `Submitted` → `Interview Scheduled` → `Offer`
- PDF file paths written to the Notes field for easy access from any device

### LinkedIn Easy Apply Automation
- Reads "Ready to Apply" records from Airtable
- Playwright-based browser automation fills and submits LinkedIn Easy Apply forms
- Handles login, session persistence, and common screening questions
- `--dry-run` mode fills forms without submitting
- Requires `xvfb-run` on headless Linux (WSL2 / server)

### IT Support Specialised Search
- Focused search for IT support / helpdesk roles
- Verifies LinkedIn Easy Apply availability on-page before selecting
- Ensures a minimum number of verified Easy Apply jobs in the result set

### AI Agent Integration (WhatsApp & Telegram)
- Runs as an [OpenClaw](https://openclaw.ai) skill — trigger searches and tailoring directly from your phone
- Supports natural-language commands: *"Search for DevOps jobs posted this week"*
- Send a job URL in chat → receive an Airtable link with the tailored PDFs ready to download
- No cover letter wall of text in chat — just a clean notification with the review link

---

## Architecture

```
WhatsApp / Telegram
       │
       ▼
 OpenClaw Agent  (gpt-4o-mini)
       │
       ├─ Job URL received ──────────► tailor_from_url.py
       │                                      │
       └─ Search command ────────────► job_pipeline.py
                                              │
                    ┌─────────────────────────┴──────────────────────────┐
                    │                                                     │
              JSearch API                                          ai_tailoring.py
         (LinkedIn / Indeed /                               (resume + cover letter
          Glassdoor / ZipRecruiter)                          GPT-4o-mini engine)
                    │                                                     │
                    └─────────────────────────┬──────────────────────────┘
                                              │
                                       pdf_generator.py
                                    (tailored resume + CL PDFs)
                                              │
                                       airtable_sync.py
                                    (Pending Review record + PDF paths)
                                              │
                                    Airtable "Job Applications"
                                              │
                              linkedin_apply.py (Easy Apply automation)
```

---

## Setup

### Prerequisites

- Python 3.9+
- A virtual environment with dependencies installed (see below)
- `xvfb-run` if running LinkedIn Easy Apply on a headless server

```bash
python -m venv ~/job_venv
source ~/job_venv/bin/activate
pip install openai reportlab Pillow playwright
playwright install chromium
```

### API Keys Required

| Service | Purpose | Where to get it |
|---|---|---|
| OpenAI | GPT-4o-mini scoring, tailoring, cover letters | [platform.openai.com](https://platform.openai.com) |
| RapidAPI (JSearch) | Job search across 5 platforms | [rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) |
| Airtable PAT | Job tracking base | [airtable.com/create/tokens](https://airtable.com/create/tokens) — scopes: `data.records:write`, `schema.tables:write` |
| LinkedIn (optional) | Easy Apply automation | Your own credentials |

Store credentials in `~/.openclaw/agents/main/agent/auth-profiles.json`:

```json
{
  "profiles": {
    "openai:default":    { "key": "sk-..." },
    "rapidapi:default":  { "key": "your-rapidapi-key" },
    "airtable:default":  { "key": "patXXXXXXXXXXXXXX", "base_id": "appXXXXXXXXXXXXXX" },
    "linkedin:default":  { "email": "you@example.com", "password": "..." }
  }
}
```

### Profile Setup

Copy the template and fill in your details:

```bash
cp profile_template.json ~/job_profile.json
```

Key sections in `job_profile.json`:

```json
{
  "profile": {
    "personal":     { "full_name", "email", "phone", "location", "linkedin_url", "github_url" },
    "experience":   { "years_total", "current_title", "work_history": [...] },
    "education":    [...],
    "skills":       { "programming_languages", "frameworks", "tools", "certifications", "skill_groups" },
    "preferences":  { "salary_expectations", "work_arrangement" },
    "wins":         ["Achievement 1", "Achievement 2"]
  },
  "search_criteria": {
    "job_titles": ["DevOps Engineer", "Cloud Engineer", "SRE"]
  }
}
```

### Airtable Setup

1. Create a base at [airtable.com](https://airtable.com) named **Job Pipeline**
2. Copy the base ID from the URL (starts with `app`)
3. Add it to `auth-profiles.json` under `airtable:default.base_id`
4. Run the setup command to auto-create the table schema:

```bash
python airtable_sync.py --base appXXXXXXXXXXXXXX --setup
```

---

## Usage

### Full Pipeline (Search → Score → Tailor → PDFs → Airtable)

```bash
# Default: all profile job titles, last month, all boards, 70% threshold
python job_pipeline.py

# Stricter matching — 80% threshold
python job_pipeline.py 0.80

# Specific query, recent postings, experience filter
python job_pipeline.py --query "devops engineer" --days 3 --experience "3-4"

# Multiple custom titles, LinkedIn only, this week
python job_pipeline.py --titles "Cloud Engineer,SRE,Platform Engineer" --days 7 --boards linkedin

# Fast mode — skip PDFs and Airtable sync
python job_pipeline.py --no-pdf --no-airtable
```

#### CLI Flags

| Flag | Example | Description |
|---|---|---|
| `0.75` | `python job_pipeline.py 0.80` | Minimum match score (default: 0.70) |
| `--query` | `--query "cloud engineer"` | Search a single query instead of all profile titles |
| `--titles` | `--titles "SRE,Platform Engineer"` | Comma-separated list of custom job titles |
| `--days` | `--days 3` | Only jobs posted in last N days (maps to: 1→today, 3→3days, 7→week, 30→month) |
| `--experience` | `--experience "3-4"` | Target experience range; penalises jobs outside it |
| `--boards` | `--boards linkedin,indeed` | Limit to specific job boards |
| `--no-pdf` | | Skip PDF generation |
| `--no-airtable` | | Skip Airtable sync |

### Tailor from a Job URL

```bash
python tailor_from_url.py "https://www.linkedin.com/jobs/view/4138977648/"
python tailor_from_url.py "https://jobs.lever.co/company/role-id"
python tailor_from_url.py "https://boards.greenhouse.io/company/jobs/12345"

# Skip PDF generation (faster)
python tailor_from_url.py "URL" --no-pdf
```

Output: Tailored resume PDF + cover letter PDF + Airtable record with status "Pending Review".

### IT Support Specialised Search

```bash
python it_support_ea_search.py
```

Searches for IT support / helpdesk roles posted within the last 3 days, verifies LinkedIn Easy Apply availability, and selects the top 5 matches (minimum 2 verified Easy Apply). Results are synced to Airtable.

### LinkedIn Easy Apply Automation

Once jobs have "Ready to Apply" status in Airtable:

```bash
# Headed (local desktop)
python linkedin_apply.py

# Headless server / WSL2 (requires xvfb)
xvfb-run -a python linkedin_apply.py

# Dry run — fills forms but does not submit
python linkedin_apply.py --dry-run

# Apply to at most 5 jobs
python linkedin_apply.py --limit 5
```

### Standalone Airtable Sync

```bash
# Sync today's match JSON to Airtable
python airtable_sync.py

# Sync a specific JSON file
python airtable_sync.py --json ~/job_applications/2026-03-02_matches.json

# Just create/verify the table schema
python airtable_sync.py --setup
```

---

## AI Agent Integration

When installed as an [OpenClaw](https://openclaw.ai) skill, the pipeline is fully controllable from WhatsApp or Telegram.

### Trigger a Job Search

Send natural language to your agent:

| Message | What runs |
|---|---|
| `Search for DevOps jobs` | `job_pipeline.py --query "devops engineer"` |
| `Find cloud engineer roles posted this week` | `job_pipeline.py --query "cloud engineer" --days 7` |
| `Search for IT support jobs requiring 3-4 years, last 3 days` | `job_pipeline.py --query "IT support specialist" --days 3 --experience "3-4"` |
| `Run a fresh search with strict matching` | `job_pipeline.py 0.80` |
| `LinkedIn only, senior DevOps, posted today` | `job_pipeline.py --query "devops engineer" --days 1 --boards linkedin` |

The agent replies with the top 5 matches: score, title, company, and salary range.

### Tailor from a URL

Paste any job posting URL into the chat:

```
https://www.linkedin.com/jobs/view/4138977648/
https://www.dice.com/job-detail/d36041bc-2a9b-40a5-ab95-5ffe8532088c
https://boards.greenhouse.io/company/jobs/12345
```

The agent runs `tailor_from_url.py`, generates PDFs, syncs to Airtable, and replies with a short notification:

```
✅ Tailored for: DevOps Engineer at Acme Corp
Email subject: DevOps Engineer — Benjamin Mbugua
Review & download PDFs: https://airtable.com/appXXX/tblXXX/recXXX
```

Open the Airtable link to read the full cover letter, download the PDFs, and update the status when ready to apply.

---

## Output Files

```
~/job_applications/
├── YYYY-MM-DD_matches.csv          # All scored jobs (score, title, company, salary, URL)
├── YYYY-MM-DD_matches.json         # Full pipeline output including cover letters
├── YYYY-MM-DD_cover_letters/       # Text cover letters + keyword maps
│   ├── Acme_Corp_DevOps_Engineer.txt
│   └── ...
└── YYYY-MM-DD/
    └── pdfs/                       # Tailored resume + cover letter PDFs per job
        ├── resume_Acme_Corp_DevOps_Engineer.pdf
        ├── cover_letter_Acme_Corp_DevOps_Engineer.pdf
        └── ...
```

---

## Airtable Schema

The `Job Applications` table is auto-created with the following fields:

| Field | Type | Description |
|---|---|---|
| Job Title | Single line | Role name |
| Company | Single line | Employer |
| Score | Number | GPT match score (0–100) |
| Location | Single line | City, state or "Remote" |
| Salary | Single line | Salary range if listed |
| Platform | Single line | linkedin / indeed / glassdoor / url-tailor |
| Match Reasons | Long text | Why this job matches the profile |
| Skill Gaps | Long text | Missing skills or experience |
| Apply URL | URL | Direct link to job posting |
| Cover Letter | Long text | Full cover letter text |
| Resume PDF | Attachment | Tailored resume PDF |
| Cover Letter PDF | Attachment | Cover letter PDF |
| Status | Single select | Pending Review → Ready to Apply → Submitted → Interview Scheduled → Offer |
| Applied Date | Date | Set when status changes to Submitted |
| Notes | Long text | PDF file paths + miscellaneous notes |

---

## Project Structure

```
job-auto-apply/
├── job_pipeline.py          # Main pipeline: search → score → tailor → PDF → Airtable
├── tailor_from_url.py       # URL-based tailoring: fetch → extract → tailor → Airtable
├── ai_tailoring.py          # GPT-4o-mini resume + cover letter tailoring engine (v2)
├── airtable_sync.py         # Airtable create/update/query helpers
├── pdf_generator.py         # reportlab PDF generation (resume + cover letter)
├── linkedin_apply.py        # Playwright Easy Apply automation
├── it_support_ea_search.py  # IT support focused search with EA verification
├── indeed_jobs.py           # Indeed-specific search helpers
├── job_search_apply.py      # Core abstractions (ApplicantProfile, JobSearchParams)
├── drive_uploader.py        # Google Drive upload helper (optional)
├── gmail_tracker.py         # Gmail application tracker (optional)
├── sheets_sync.py           # Google Sheets sync (optional)
├── profile_template.json    # Starter profile — copy to ~/job_profile.json
├── platform_integration.md  # Technical notes on API integration + scraping
└── SKILL.md                 # OpenClaw skill definition + agent invocation guide
```

---

## Security Notes

The following files are excluded from version control (`.gitignore`):

- `credentials.json`, `drive_token.json`, `sheets_token.json` — OAuth tokens
- `linkedin_session.json` — Playwright session cookies
- `auth-profiles.json` — API keys and account credentials
- `application_results.json` — Personal application data

**Never commit credentials.** Store them in `auth-profiles.json` locally.

---

## Supported Job Platforms

| Platform | Search | Easy Apply |
|---|---|---|
| LinkedIn | ✅ via JSearch | ✅ Playwright automation |
| Indeed | ✅ via JSearch | — |
| Glassdoor | ✅ via JSearch | — |
| ZipRecruiter | ✅ via JSearch | — |
| Wellfound (AngelList) | ✅ via JSearch | — |
| Greenhouse / Lever / Workday | ✅ URL tailor | — |
| Dice | ✅ URL tailor (may block scrapers) | — |
