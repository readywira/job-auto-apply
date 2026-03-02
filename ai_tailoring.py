#!/usr/bin/env python3
"""
AI Resume & Cover Letter Tailoring Engine — v2 prompts
Per-job tailored resume + ATS-optimised cover letters.

Used by job_pipeline.py — not run directly.

Resume Tailoring v2:
  - action + scope + tools + outcome structure for every bullet
  - metric hooks when hard numbers aren't available
  - keyword alignment without stuffing
  - output: structured JSON (summary, experience bullets, skills, keyword map, questions)

Cover Letter v2:
  - 250-350 words, 3 paras + 3 bullet highlights
  - references 2-3 specific JD requirements with proof
  - no clichés; closes with CTA + availability
  - output: cover letter text, subject line, 5 swap tokens
"""

import json, os, urllib.request

PROFILE_PATH = os.path.expanduser("~/job_profile.json")
AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")


# ── GPT helper (JSON mode) ────────────────────────────────────────────────────
def _gpt_json(openai_key, prompt, max_tokens=1600):
    """Call gpt-4o-mini with JSON response format. Returns parsed dict."""
    data = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {openai_key}",
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"].strip()
    return json.loads(raw)


# ── Base resume formatter ─────────────────────────────────────────────────────
def format_base_resume(profile):
    """Convert job_profile.json to labelled text for GPT context."""
    p   = profile
    per = p["personal"]
    exp = p["experience"]

    lines = [
        f"Name: {per['full_name']}",
        f"Title: {exp.get('current_title', '')} | {per['location']['city']}, {per['location']['state']}",
        f"Contact: {per['email']} | {per['phone']}",
        "",
        "EXPERIENCE:",
    ]
    for i, job in enumerate(p.get("work_history", []), 1):
        lines.append(
            f"[{i}] {job['company']} | {job.get('location','')} | {job['start']} \u2013 {job['end']}"
        )
        lines.append(f"    Role: {job['title']}")
        for b in job.get("bullets", []):
            lines.append(f"    \u2022 {b}")
        lines.append("")

    lines.append("EDUCATION:")
    for edu in p.get("education", []):
        lines.append(
            f"  \u2022 {edu['institution']} ({edu['start']} \u2013 {edu['end']}): {edu['degree']}"
        )
    lines.append("")

    lines.append("CERTIFICATIONS:")
    for c in p.get("skills", {}).get("certifications", []):
        lines.append(f"  \u2022 {c}")
    lines.append("")

    lines.append("SKILLS:")
    for g in p.get("skills", {}).get("skill_groups", []):
        lines.append(f"  {g['label']}: {g['items']}")

    return "\n".join(lines)


# ── Resume Tailoring v2 ───────────────────────────────────────────────────────
_RESUME_SYSTEM = """\
You are an expert resume writer for US-based tech roles. Optimise for ATS and hiring managers.

HARD RULES:
1. No generic filler ("Demonstrated expertise", "Responsible for", "Worked on", "Leveraged").
2. Every bullet = strong verb + concrete artifact/system + tools used + impact/metric.
3. Do NOT invent employers, certifications, or metrics.
4. If a metric is missing add a metric hook: e.g. "(tracked via Core Web Vitals; metric available on request)".
5. Mirror JD language naturally — keyword alignment, no keyword stuffing.
6. Skills section: only skills that appear in the JD or are critical adjacencies.
7. No bullet repeats the same starter verb more than twice.
8. At least 70 % of bullets must contain measurable impact OR a metric hook.
9. Reframe bullets to match the JD — never invent new responsibilities.\
"""


def tailor_resume(openai_key, profile, job_description, company, job_title):
    """
    Tailor resume bullets/summary/skills to a specific job description.

    Returns dict:
      summary       – str: 2-sentence tailored professional summary
      experience    – list[{"company": str, "tailored_bullets": [str]}]
                      One entry per employer, same order as base resume
      skills        – list[{"label": str, "items": str}]
      keyword_map   – list[["JD requirement", "section + bullet location"]]
      questions     – list[str]: up to 6 questions for missing metrics
    """
    base_resume = format_base_resume(profile)

    prompt = f"""{_RESUME_SYSTEM}

=== BASE RESUME ===
{base_resume}

=== TARGET JOB DESCRIPTION ===
{job_description[:2200]}

COMPANY: {company}
ROLE: {job_title}

Return a JSON object with EXACTLY these keys:
{{
  "summary": "Tailored 2-sentence professional summary referencing specific JD needs",
  "experience": [
    {{
      "company": "<exact company name from base resume>",
      "tailored_bullets": [
        "<strong verb> <artifact/system/feature> using <tool(s)>, achieving <metric or metric hook>"
      ]
    }}
  ],
  "skills": [
    {{"label": "<category>", "items": "<comma-separated JD-aligned skills>"}}
  ],
  "keyword_map": [
    ["<JD requirement phrase>", "<Resume section + bullet that covers it>"]
  ],
  "questions": [
    "<What metric is available for [bullet description]?>"
  ]
}}

experience array must have ONE entry per employer in the base resume, same order.
tailored_bullets: 3-5 bullets per employer, reframed for this JD.
Return ONLY valid JSON, no markdown fences."""

    try:
        return _gpt_json(openai_key, prompt, max_tokens=1600)
    except Exception as e:
        # Graceful fallback: return empty tailoring so pipeline can continue
        return {
            "summary": "",
            "experience": [],
            "skills": [],
            "keyword_map": [],
            "questions": [],
            "_error": str(e),
        }


# ── Cover Letter v2 ───────────────────────────────────────────────────────────
_CL_SYSTEM = """\
You are a senior tech hiring manager + career coach writing tailored cover letters.
Be confident, specific, and skimmable. No clichés (no "passionate", "dynamic", "synergy").\
"""


def write_cover_letter(openai_key, profile, job_description, company, job_title,
                       tailored_summary=""):
    """
    Generate a v2 cover letter.

    Returns dict:
      cover_letter – str: full letter, 250-350 words, plain text
      subject_line – str: email subject line
      swap_tokens  – list[{"token": str, "current": str, "hint": str}]
    """
    per = profile["personal"]
    exp = profile["experience"]

    wins = profile.get("wins", [
        "Improved Core Web Vitals scores through targeted performance optimisation",
        "Delivered all UI milestones on schedule with 100 % client approval",
        "Streamlined data collection workflows, reducing processing time",
    ])
    wins_text = "\n".join(f"\u2022 {w}" for w in wins[:3])

    ref_summary = f"\nCANDIDATE SUMMARY: {tailored_summary}" if tailored_summary else ""

    prompt = f"""{_CL_SYSTEM}

=== JOB DESCRIPTION ===
{job_description[:2200]}

COMPANY: {company}
ROLE: {job_title}

CANDIDATE:
Name: {per['full_name']}
Current Title: {exp.get('current_title', '')}
Location: {per['location']['city']}, {per['location']['state']}
Email: {per['email']} | Phone: {per['phone']}
LinkedIn: {per.get('linkedin_url', '')}
{ref_summary}

TOP WINS (use as proof points):
{wins_text}

STRUCTURE (hard requirement — all 3 parts required):
  Para 1 (3-4 sentences): Who I am + specific experience relevant to THIS role + why THIS company.
  Para 2: Exactly 3 bullet highlights, each 2 sentences long (format: • highlight sentence one. Highlight sentence two.)
            Each bullet references a specific JD requirement and maps it to a proof point.
  Para 3 (3 sentences): Expand on what you bring + clear call-to-action + availability.

RULES:
  • MINIMUM 250 words, TARGET 300 words — count carefully before finalising
  • Para 1 alone should be ~80 words
  • Each bullet highlight should be ~40-50 words
  • Para 3 should be ~50-60 words
  • No placeholders like [Your Name] or [Insert metric]
  • Must name the actual company and role by name
  • No bullet repeats a verb used in another bullet
  • Close with: {per['full_name']} | {per['email']} | {per['phone']}

Return JSON with EXACTLY these keys:
{{
  "cover_letter": "<full cover letter text, 250-350 words>",
  "subject_line": "<email subject: Application for {job_title} \u2013 {per['full_name']}>",
  "swap_tokens": [
    {{"token": "COMPANY_NEED_1", "current": "<what was written>", "hint": "<what to swap if company differs>"}},
    {{"token": "WIN_METRIC",     "current": "<metric used>",       "hint": "<replace with real number if available>"}},
    {{"token": "TOOL_STACK",     "current": "<tools mentioned>",   "hint": "<swap for JD tools>"}},
    {{"token": "ROLE_HOOK",      "current": "<opening hook>",      "hint": "<tailor to hiring manager's focus>"}},
    {{"token": "CTA_DATE",       "current": "two weeks",           "hint": "<replace with actual start availability>"}}
  ]
}}
Return ONLY valid JSON, no markdown fences."""

    try:
        return _gpt_json(openai_key, prompt, max_tokens=1200)
    except Exception as e:
        return {
            "cover_letter": f"[Cover letter generation failed: {e}]",
            "subject_line": f"Application for {job_title} – {per['full_name']}",
            "swap_tokens": [],
            "_error": str(e),
        }
