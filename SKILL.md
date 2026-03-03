---
name: job-auto-apply
description: >
  Automated job search and application pipeline for Benjamin. Use when the user says anything like:
  "run a fresh job search", "find new jobs", "search for jobs", "run the job pipeline",
  "find and apply to jobs", "auto-apply for [job title]", "search for [position] jobs and apply",
  "help me apply to multiple jobs", "run job search", "start job search", "check for new jobs",
  "search for devops jobs posted in the last 3 days", "find IT support roles requiring 3-4 years
  experience", or any job search with specific criteria like title, experience, recency.
  Also use when the user sends a job posting URL — fetch it and generate a tailored resume
  and cover letter for that specific job.
---

# Job Auto-Apply Skill

Automate job searching and application submission across multiple job platforms using Clawdbot.

## ⚡ Agent Invocation (WhatsApp / Telegram)

### Handling a Job Posting URL

**If the user's message contains a URL (starts with http)**, this is a URL-tailor request.

⚠️ **IMPORTANT**: Do NOT use your built-in `web_fetch` tool to fetch the URL yourself.
You MUST execute the bash command below. The script handles fetching internally.

Run via bash exec:
```
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/tailor_from_url.py "URL_HERE"
```

**After running, relay the NOTIFICATION block from the output.**
Look for the section between `NOTIFICATION` and the final `===`. Send only that — do NOT paste the full cover letter.

Example reply to user:
> ✅ Tailored for: DevOps Engineer at Acme Corp
> Email subject: DevOps Engineer — Benjamin Mbugua
> Review & download PDFs: https://airtable.com/appXXX/tblXXX/recXXX

**If the script output says "Fetch failed" or the page was blocked:**
Reply: "I couldn't fetch that page directly (the site blocks bots). Please paste the job description text and I'll tailor everything from that."

Example trigger messages:
- "https://www.linkedin.com/jobs/view/..."
- "https://www.dice.com/job-detail/..."
- "tailor my resume for this: https://..."
- "write a cover letter for this job: https://..."

---

### Running a Job Search

**When the user asks to search for jobs** (with or without specific criteria), translate their
natural language into CLI flags and run:

```bash
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/job_pipeline.py [FLAGS]
```

**Natural language → CLI flags:**

| User says | CLI flag to add |
|---|---|
| "devops jobs" / "search for devops" | `--query "devops engineer"` |
| "IT support roles" | `--query "IT support specialist"` |
| "cloud engineer positions" | `--query "cloud engineer"` |
| "posted in last 3 days" / "recent jobs" | `--days 3` |
| "posted today" | `--days 1` |
| "posted this week" | `--days 7` |
| "3-4 years experience" | `--experience "3-4"` |
| "mid-level" | `--experience "3-5"` |
| "senior" | `--experience "5-8"` |
| "LinkedIn only" | `--boards linkedin` |
| "Indeed and LinkedIn" | `--boards linkedin,indeed` |
| "strict matching" / "best matches only" | `0.80` (score threshold) |

**Examples:**

```bash
# "search for devops jobs posted in last 3 days requiring 3-4 years experience"
... job_pipeline.py --query "devops engineer" --days 3 --experience "3-4"

# "find cloud engineer jobs on LinkedIn posted this week"
... job_pipeline.py --query "cloud engineer" --days 7 --boards linkedin

# "run a fresh search with strict matching"
... job_pipeline.py 0.80

# Default full search (all profile job titles, last month)
... job_pipeline.py
```

**Optional flags:**
```bash
--no-airtable    # Skip Airtable sync (faster)
--no-pdf         # Skip PDF generation (fastest)
--boards X,Y     # Limit to specific job boards
```

**After running, report back:**
- Total jobs found and scored
- Number of good matches (above threshold)
- Top 5 matches: score · title · company · salary
- Any errors

---

**IT Support focused search** (finds verified LinkedIn Easy Apply jobs):
```bash
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/it_support_ea_search.py
```

**LinkedIn Easy Apply submission** (applies to Airtable "Ready to Apply" jobs):
```bash
xvfb-run -a /home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/linkedin_apply.py
```
> Note: `linkedin_apply.py` requires `xvfb-run` since it launches a browser. Only LinkedIn URLs will be processed; others are skipped automatically.

**Profile and output paths:**
- Profile: `/home/benji/job_profile.json`
- Output: `/home/benji/job_applications/`
- Cover letters: `/home/benji/job_applications/YYYY-MM-DD_cover_letters/`
- PDFs: `/home/benji/job_applications/YYYY-MM-DD/pdfs/`

## Overview

This skill enables automated job search and application workflows. It searches for jobs matching user criteria, analyzes compatibility, generates tailored cover letters, and submits applications automatically or with user confirmation.

**Supported Platforms:**
- LinkedIn (including Easy Apply)
- Indeed
- Glassdoor
- ZipRecruiter
- Wellfound (AngelList)

## Quick Start

### 1. Set Up User Profile

First, create a user profile using the template:

```bash
# Copy the profile template
cp profile_template.json ~/job_profile.json

# Edit with user's information
# Fill in: name, email, phone, resume path, skills, preferences
```

### 2. Run Job Search and Apply

```bash
# Standard full pipeline (search → score → cover letters → PDFs → Airtable)
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/job_pipeline.py

# Fast mode — skip PDFs and Airtable
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/job_pipeline.py --no-pdf --no-airtable

# Stricter matching (80% threshold)
/home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/job_pipeline.py 0.80

# LinkedIn Easy Apply (requires Airtable "Ready to Apply" jobs + xvfb)
xvfb-run -a /home/benji/job_venv/bin/python3 /home/benji/.openclaw/workspace/skills/job-auto-apply/linkedin_apply.py
```

## Workflow Steps

### Step 1: Profile Configuration

Load the user's profile from the template or create programmatically:

```python
from job_search_apply import ApplicantProfile

profile = ApplicantProfile(
    full_name="Jane Doe",
    email="jane@example.com",
    phone="+1234567890",
    resume_path="~/Documents/resume.pdf",
    linkedin_url="https://linkedin.com/in/janedoe",
    years_experience=5,
    authorized_to_work=True,
    requires_sponsorship=False
)
```

### Step 2: Define Search Parameters

```python
from job_search_apply import JobSearchParams, JobPlatform

search_params = JobSearchParams(
    title="Software Engineer",
    location="Remote",
    remote=True,
    experience_level="mid",
    job_type="full-time",
    salary_min=100000,
    platforms=[JobPlatform.LINKEDIN, JobPlatform.INDEED]
)
```

### Step 3: Run Automated Application

```python
from job_search_apply import auto_apply_workflow

results = auto_apply_workflow(
    search_params=search_params,
    profile=profile,
    max_applications=10,
    min_match_score=0.75,
    dry_run=False,
    require_confirmation=True
)
```

## Integration with Clawdbot

### Using as a Clawdbot Tool

When installed as a Clawdbot skill, invoke via natural language:

**Example prompts:**
- "Find and apply to Python developer jobs in San Francisco"
- "Search for remote backend engineer positions and apply to the top 5 matches"
- "Auto-apply to senior software engineer roles with 100k+ salary"
- "Apply to jobs at tech startups on Wellfound"

The skill will:
1. Parse the user's intent and extract search parameters
2. Load the user's profile from saved configuration
3. Search across specified platforms
4. Analyze job compatibility
5. Generate tailored cover letters
6. Submit applications (with confirmation if enabled)
7. Report results and track applications

### Configuration in Clawdbot

Add to your Clawdbot configuration:

```json
{
  "skills": {
    "job-auto-apply": {
      "enabled": true,
      "profile_path": "~/job_profile.json",
      "default_platforms": ["linkedin", "indeed"],
      "max_daily_applications": 10,
      "require_confirmation": true,
      "dry_run": false
    }
  }
}
```

## Features

### 1. Multi-Platform Search
- Searches across all major job platforms
- Uses official APIs when available
- Falls back to web scraping for platforms without APIs

### 2. Smart Matching
- Analyzes job descriptions for requirement matching
- Calculates compatibility scores
- Filters jobs based on minimum match threshold

### 3. Application Customization
- Generates tailored cover letters per job
- Customizes resume emphasis based on job requirements
- Handles platform-specific application forms

### 4. Safety Features
- **Dry Run Mode**: Test without submitting applications
- **Manual Confirmation**: Review each application before submission
- **Rate Limiting**: Prevents overwhelming platforms
- **Application Logging**: Tracks all submissions for reference

### 5. Form Automation
Automatically fills common application fields:
- Personal information
- Work authorization status
- Education and experience
- Skills and certifications
- Screening questions (using AI when needed)

## Advanced Usage

### Custom Cover Letter Templates

Create a template with placeholders:

```text
Dear Hiring Manager at {company},

I am excited to apply for the {position} role. With {years} years of 
experience in {skills}, I believe I would be an excellent fit.

{custom_paragraph}

I look forward to discussing how I can contribute to {company}'s success.

Best regards,
{name}
```

### Application Tracking

Results are automatically saved in JSON format with details on each application submitted, including timestamps, match scores, and status.

## Bundled Resources

### Scripts
- `job_pipeline.py` - **Main pipeline**: search → score → cover letters → PDFs → Airtable sync
- `it_support_ea_search.py` - IT support focused search with LinkedIn Easy Apply verification
- `linkedin_apply.py` - LinkedIn Easy Apply automation (reads from Airtable "Ready to Apply")
- `airtable_sync.py` - Standalone Airtable sync utility
- `pdf_generator.py` - Resume + cover letter PDF generation
- `ai_tailoring.py` - AI-powered resume tailoring per job description

### References
- `platform_integration.md` - Technical documentation for API integration, web scraping, form automation, and platform-specific details

### Assets
- `profile_template.json` - Comprehensive profile template with all required and optional fields

## Safety and Ethics

### Important Guidelines

1. **Truthfulness**: Never misrepresent qualifications or experience
2. **Genuine Interest**: Only apply to jobs you're actually interested in
3. **Rate Limiting**: Respect platform limits and terms of service
4. **Manual Review**: Consider enabling confirmation mode for quality control
5. **Privacy**: Secure storage of personal information and credentials

### Best Practices

- Start with dry-run mode to verify behavior
- Set reasonable limits (5-10 applications per day)
- Use high match score thresholds (0.75+)
- Enable confirmation for important applications
- Track results to optimize strategy
