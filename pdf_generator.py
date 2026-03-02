#!/usr/bin/env python3
"""
PDF Generator — Benjamin's Job Application Pipeline
Produces resume.pdf and per-job cover letter PDFs.
Styling matches the provided sample (Times serif, hanging bullets, italic titles).

Usage:
    python pdf_generator.py                           # resume only
    python pdf_generator.py --from-json matches.json  # batch covers
    python pdf_generator.py --cover "Company" "Title" "Cover text..."
"""

import json, os, sys, re
from datetime import datetime

PROFILE_PATH = os.path.expanduser("~/job_profile.json")
OUTPUT_BASE  = os.path.expanduser("~/job_applications")
TODAY        = datetime.now().strftime("%Y-%m-%d")
PDF_DIR      = os.path.join(OUTPUT_BASE, TODAY, "pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
        Table, TableStyle, KeepTogether
    )
except ImportError:
    print("ERROR: reportlab not installed. Run: pip install reportlab Pillow")
    sys.exit(1)

# ── Colors ────────────────────────────────────────────────────────────────────
INK   = colors.black
MUTED = colors.HexColor("#444444")

# ── Page layout ───────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = letter
MARGIN_H  = 0.75 * inch
MARGIN_V  = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN_H

# ── Font family: Times (serif, matches sample) ────────────────────────────────
_BODY     = "Times-Roman"
_BOLD     = "Times-Bold"
_ITALIC   = "Times-Italic"
_BOLDITAL = "Times-BoldItalic"

NAME_PT    = 20
BASE_PT    = 11
SECTION_PT = 12
SMALL_PT   = 10


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe(s):
    return re.sub(r"[^\w\-]", "_", s).strip("_")[:50]


def load_profile():
    with open(PROFILE_PATH) as f:
        return json.load(f)["profile"]


# ── Paragraph styles ──────────────────────────────────────────────────────────
def styles():
    S = {}

    # Name: large bold serif, mixed case
    S["name"] = ParagraphStyle(
        "name",
        fontName=_BOLD,
        fontSize=NAME_PT,
        leading=24,
        textColor=INK,
        spaceAfter=2,
    )
    # Tagline: normal weight under name
    S["tagline"] = ParagraphStyle(
        "tagline",
        fontName=_BODY,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
        spaceAfter=1,
    )
    # Contact line: slightly muted, small
    S["contact"] = ParagraphStyle(
        "contact",
        fontName=_BODY,
        fontSize=SMALL_PT,
        leading=13,
        textColor=MUTED,
        spaceAfter=8,
    )
    # Section header: bold, slightly larger
    S["section"] = ParagraphStyle(
        "section",
        fontName=_BOLD,
        fontSize=SECTION_PT,
        leading=15,
        textColor=INK,
        spaceBefore=8,
        spaceAfter=2,
    )
    # Company name: bold (left of two-col)
    S["org_left"] = ParagraphStyle(
        "org_left",
        fontName=_BOLD,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
    )
    # Location: normal (right of two-col)
    S["org_right"] = ParagraphStyle(
        "org_right",
        fontName=_BODY,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
        alignment=TA_RIGHT,
    )
    # Job title: ITALIC (left of two-col) — matches sample
    S["role_left"] = ParagraphStyle(
        "role_left",
        fontName=_ITALIC,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
    )
    # Dates: normal (right of two-col)
    S["role_right"] = ParagraphStyle(
        "role_right",
        fontName=_BODY,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
        alignment=TA_RIGHT,
    )
    # Body paragraph
    S["body"] = ParagraphStyle(
        "body",
        fontName=_BODY,
        fontSize=BASE_PT,
        leading=15,
        textColor=INK,
        spaceAfter=4,
        alignment=TA_JUSTIFY,
    )
    # Bullet: hanging indent, supports <b> inline tags
    S["bullet"] = ParagraphStyle(
        "bullet",
        fontName=_BODY,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
        leftIndent=16,
        firstLineIndent=-10,
        spaceBefore=2,
        spaceAfter=2,
    )
    # Education institution: bold left
    S["edu_left"] = ParagraphStyle(
        "edu_left",
        fontName=_BOLD,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
    )
    # Degree: italic left
    S["edu_degree"] = ParagraphStyle(
        "edu_degree",
        fontName=_ITALIC,
        fontSize=BASE_PT,
        leading=14,
        textColor=INK,
    )
    # Cover letter styles
    S["cl_date"] = ParagraphStyle(
        "cl_date", fontName=_BODY, fontSize=BASE_PT,
        leading=14, textColor=MUTED, spaceAfter=14,
    )
    S["cl_recipient"] = ParagraphStyle(
        "cl_recipient", fontName=_BODY, fontSize=BASE_PT,
        leading=15, textColor=INK, spaceAfter=14,
    )
    S["cl_body"] = ParagraphStyle(
        "cl_body", fontName=_BODY, fontSize=BASE_PT,
        leading=16, textColor=INK, spaceAfter=10, alignment=TA_JUSTIFY,
    )
    S["cl_sig"] = ParagraphStyle(
        "cl_sig", fontName=_BODY, fontSize=BASE_PT,
        leading=14, textColor=INK, spaceAfter=2,
    )
    return S


# ── Two-column row helper ─────────────────────────────────────────────────────
def two_col(left_text, right_text, left_style, right_style, space_after=0):
    """Left text + right-aligned text on the same line (mimics LaTeX hfill)."""
    tbl = Table(
        [[Paragraph(left_text, left_style), Paragraph(right_text, right_style)]],
        colWidths=[CONTENT_W * 0.65, CONTENT_W * 0.35],
        spaceAfter=space_after,
    )
    tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    return tbl


# ── Section header with rule ──────────────────────────────────────────────────
def section_header(label, S):
    return [
        Paragraph(label, S["section"]),
        HRFlowable(width=CONTENT_W, thickness=0.7, color=INK,
                   spaceBefore=2, spaceAfter=5),
    ]


# ── Bullet paragraph (proper • with hanging indent) ───────────────────────────
def bullet_para(text, S):
    """
    Renders a single bullet line with hanging indent.
    Supports inline <b>bold</b> and <i>italic</i> tags.
    Uses en-space (U+2002) after bullet for consistent spacing.
    """
    return Paragraph(f"\u2022\u2002{text}", S["bullet"])


def bullet_list(items, S):
    return [bullet_para(item, S) for item in items]


# ── Page callbacks: name header on page 2+, page numbers ─────────────────────
def _page_callbacks(full_name):
    def on_first_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(_BODY, 9)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(PAGE_W / 2, MARGIN_V / 2 - 4, str(doc.page))
        canvas.restoreState()

    def on_later_pages(canvas, doc):
        canvas.saveState()
        # Italic name header top-left
        canvas.setFont(_ITALIC, SMALL_PT)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN_H, PAGE_H - MARGIN_V / 2, full_name)
        # Page number bottom-centre
        canvas.setFont(_BODY, 9)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(PAGE_W / 2, MARGIN_V / 2 - 4, str(doc.page))
        canvas.restoreState()

    return on_first_page, on_later_pages


# ── Shared header block ───────────────────────────────────────────────────────
def _build_header(p, S):
    per = p["personal"]
    exp = p["experience"]
    li  = per.get("linkedin_url", "").replace("https://www.linkedin.com/in/", "linkedin/").rstrip("/")
    tagline = exp.get("tagline", f"{exp.get('current_title', '')}, Seattle, USA")
    # Single em-dash (—) separators, not triple
    contact = f"{li} \u2014 {per['email']} \u2014 {per['phone']}"
    return [
        Paragraph(per["full_name"], S["name"]),      # mixed case, NOT all caps
        Paragraph(tagline, S["tagline"]),
        Paragraph(contact, S["contact"]),
    ]


# ── Generate Resume PDF ───────────────────────────────────────────────────────
def generate_resume(out_path=None):
    p = load_profile()
    S = styles()

    if not out_path:
        out_path = os.path.join(PDF_DIR, "resume.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=MARGIN_H, rightMargin=MARGIN_H,
        topMargin=MARGIN_V, bottomMargin=MARGIN_V,
    )
    story = _build_header(p, S)

    # Professional Summary
    exp = p["experience"]
    story += section_header("Professional Summary", S)
    story.append(Paragraph(
        f"Results-driven {exp.get('current_title', 'Developer')} with "
        f"{exp.get('years_total', 3)}+ years of experience in web development, "
        f"user interface design, and performance optimisation. Proven expertise in "
        f"JavaScript, HTML, CSS, and WordPress development with demonstrated success "
        f"in improving user engagement and reducing load times. Skilled in building "
        f"high-performance, user-centric interfaces with strong cross-functional collaboration.",
        S["body"]
    ))

    # Experience
    story += section_header("Experience", S)
    for job in p.get("work_history", []):
        story.append(two_col(job["company"], job.get("location", ""),
                             S["org_left"], S["org_right"]))
        story.append(two_col(job["title"], f"{job['start']} \u2013 {job['end']}",
                             S["role_left"], S["role_right"], space_after=2))
        story += bullet_list(job.get("bullets", []), S)
        story.append(Spacer(1, 5))

    # Certifications
    story += section_header("Certifications", S)
    story += bullet_list(p.get("skills", {}).get("certifications", []), S)
    story.append(Spacer(1, 4))

    # Education
    story += section_header("Education", S)
    for edu in p.get("education", []):
        date_str = f"{edu.get('start', '')} \u2013 {edu.get('end', '')}"
        story.append(two_col(edu["institution"], edu.get("location", ""),
                             S["edu_left"], S["org_right"]))
        story.append(two_col(edu["degree"], date_str,
                             S["edu_degree"], S["role_right"], space_after=5))

    # Skills — bullet list with bold labels (matches sample)
    story += section_header("Skills", S)
    for grp in p.get("skills", {}).get("skill_groups", []):
        story.append(bullet_para(f"<b>{grp['label']}:</b> {grp['items']}", S))

    on_first, on_later = _page_callbacks(p["personal"]["full_name"])
    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    print(f"  ✓ Resume PDF → {out_path}")
    return out_path


# ── Generate Tailored Resume PDF ──────────────────────────────────────────────
def generate_tailored_resume(tailored_sections, company, job_title, out_path=None):
    """
    Per-job tailored resume using AI-generated sections from ai_tailoring.py.
    tailored_sections keys: summary, experience, skills
      experience: [{"company": str, "tailored_bullets": [str]}]
      skills:     [{"label": str, "items": str}]
    """
    p = load_profile()
    S = styles()

    if not out_path:
        out_path = os.path.join(PDF_DIR, f"{safe(company)}_{safe(job_title)}_resume.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=MARGIN_H, rightMargin=MARGIN_H,
        topMargin=MARGIN_V, bottomMargin=MARGIN_V,
    )
    story = _build_header(p, S)

    # Summary (AI-tailored)
    story += section_header("Professional Summary", S)
    summary = tailored_sections.get("summary", "")
    if not summary:
        exp = p["experience"]
        summary = (
            f"Results-driven {exp.get('current_title', 'Developer')} with "
            f"{exp.get('years_total', 3)}+ years of experience delivering high-impact "
            f"web development solutions."
        )
    story.append(Paragraph(summary, S["body"]))

    # Experience (AI-tailored bullets, fallback to profile bullets)
    story += section_header("Experience", S)
    tailored_exp = {
        e["company"]: e.get("tailored_bullets", [])
        for e in tailored_sections.get("experience", [])
        if e.get("company")
    }

    for job in p.get("work_history", []):
        co     = job.get("company", "")
        loc    = job.get("location", "")
        title  = job.get("title", "")
        start  = job.get("start", "")
        end    = job.get("end", "Present")

        # Try exact match then partial match (strips parenthetical suffix)
        bullets = (
            tailored_exp.get(co) or
            tailored_exp.get(co.split(" (")[0]) or
            job.get("bullets", [])
        )

        story.append(two_col(co, loc, S["org_left"], S["org_right"]))
        story.append(two_col(title, f"{start} \u2013 {end}",
                             S["role_left"], S["role_right"], space_after=2))
        story += bullet_list(bullets, S)
        story.append(Spacer(1, 5))

    # Certifications (unchanged)
    story += section_header("Certifications", S)
    story += bullet_list(p.get("skills", {}).get("certifications", []), S)
    story.append(Spacer(1, 4))

    # Education (unchanged)
    story += section_header("Education", S)
    for edu in p.get("education", []):
        date_str = f"{edu.get('start', '')} \u2013 {edu.get('end', '')}"
        story.append(two_col(edu["institution"], edu.get("location", ""),
                             S["edu_left"], S["org_right"]))
        story.append(two_col(edu["degree"], date_str,
                             S["edu_degree"], S["role_right"], space_after=5))

    # Skills (AI-tailored, bullet format with bold labels)
    story += section_header("Skills", S)
    skill_groups = tailored_sections.get("skills") or p.get("skills", {}).get("skill_groups", [])
    for grp in skill_groups:
        story.append(bullet_para(f"<b>{grp['label']}:</b> {grp['items']}", S))

    on_first, on_later = _page_callbacks(p["personal"]["full_name"])
    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    print(f"  ✓ Tailored resume → {out_path}")
    return out_path


# ── Generate Cover Letter PDF ─────────────────────────────────────────────────
def generate_cover_letter(company, job_title, cover_text, out_path=None):
    p   = load_profile()
    per = p["personal"]
    exp = p["experience"]
    S   = styles()

    if not out_path:
        out_path = os.path.join(PDF_DIR, f"{safe(company)}_{safe(job_title)}_cover.pdf")

    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=MARGIN_H, rightMargin=MARGIN_H,
        topMargin=MARGIN_V, bottomMargin=MARGIN_V,
    )

    tagline = exp.get("tagline", exp.get("current_title", ""))
    li      = per.get("linkedin_url", "").replace("https://www.linkedin.com/in/", "linkedin/").rstrip("/")
    contact = f"{li} \u2014 {per['email']} \u2014 {per['phone']}"

    story = [
        Paragraph(per["full_name"], S["name"]),
        Paragraph(tagline, S["tagline"]),
        Paragraph(contact, S["contact"]),
        HRFlowable(width=CONTENT_W, thickness=0.7, color=INK, spaceBefore=4, spaceAfter=14),
        Paragraph(datetime.now().strftime("%B %d, %Y"), S["cl_date"]),
        Paragraph(f"Hiring Manager<br/>{company}<br/>Re: {job_title}", S["cl_recipient"]),
    ]

    # Body — split on double newlines, render each paragraph
    for para in cover_text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # Skip if it's just the signature line
        if per["full_name"] in para and per["email"] in para:
            continue
        story.append(Paragraph(para.replace("\n", " "), S["cl_body"]))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Sincerely,", S["cl_sig"]))
    story.append(Spacer(1, 22))
    story.append(Paragraph(f"<b>{per['full_name']}</b>", S["cl_sig"]))
    story.append(Paragraph(f"{per['email']} \u00b7 {per['phone']}", S["cl_sig"]))
    if per.get("linkedin_url"):
        story.append(Paragraph(per["linkedin_url"], S["cl_sig"]))

    on_first, on_later = _page_callbacks(per["full_name"])
    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    print(f"  ✓ Cover PDF  → {out_path}")
    return out_path


# ── Batch from pipeline JSON ──────────────────────────────────────────────────
def batch_from_json(json_path, resume_path=None):
    with open(json_path) as f:
        jobs = json.load(f)
    if not resume_path:
        resume_path = generate_resume()
    results = []
    for entry in jobs:
        company = entry.get("company", "Company")
        title   = entry.get("title", "Role")
        cl_text = entry.get("cover_letter", "")
        if not cl_text:
            continue
        cl_path = generate_cover_letter(company, title, cl_text)
        results.append({**entry, "resume_pdf": resume_path, "cover_pdf": cl_path})
    return results, resume_path


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--cover" in sys.argv:
        idx = sys.argv.index("--cover")
        generate_cover_letter(sys.argv[idx+1], sys.argv[idx+2], sys.argv[idx+3])
    elif "--from-json" in sys.argv:
        idx = sys.argv.index("--from-json")
        results, _ = batch_from_json(sys.argv[idx+1])
        print(f"\n  Generated {len(results)} cover PDFs in {PDF_DIR}/")
    else:
        generate_resume()
        print(f"\n  Saved to: {PDF_DIR}/")
