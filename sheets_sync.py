#!/usr/bin/env python3
"""
Google Sheets Sync — Benjamin's Job Application Pipeline

Two modes:
  INTAKE workflow (new — primary):
    Paste a job URL anywhere in column A of the "Job Intake" tab.
    Run --watch (or set up a cron job) and the AI layer will:
      1. Fetch the job description from the URL
      2. Score, tailor resume + cover letter with GPT
      3. Generate PDFs
      4. Push to Airtable as "Ready to Apply"
      5. Update the sheet row (Status → Done, fills Company, Title, Score…)

  BULK review workflow (existing):
    python sheets_sync.py --push              # push today's pipeline JSON → Sheet
    python sheets_sync.py --pull              # YES rows → PDFs + Airtable

Commands:
    python sheets_sync.py --watch             # poll "Job Intake" tab every 60s
    python sheets_sync.py --add URL           # manually add one URL to intake tab
    python sheets_sync.py --push              # push pipeline matches → Applications tab
    python sheets_sync.py --pull              # YES rows → PDFs + Airtable
    python sheets_sync.py --setup             # OAuth consent only
    python sheets_sync.py --url               # print spreadsheet URL
"""

import json, os, sys, time, re, html, urllib.request
from datetime import datetime

SKILL_DIR   = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply")
CREDS_FILE  = os.path.join(SKILL_DIR, "credentials.json")
TOKEN_FILE  = os.path.join(SKILL_DIR, "sheets_token.json")
AUTH_FILE   = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
PROFILE_PATH = os.path.expanduser("~/job_profile.json")
OUTPUT_BASE = os.path.expanduser("~/job_applications")
TODAY       = datetime.now().strftime("%Y-%m-%d")

OAUTH_PORT  = 8889
SCOPES      = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_TITLE = "Job Applications — Benjamin Mbugua"

# ── Tab names ─────────────────────────────────────────────────────────────────
TAB_INTAKE  = "Job Intake"
TAB_APPS    = "Applications"

# ── Intake tab column layout (0-indexed) ─────────────────────────────────────
# Column B (Description) is optional — paste job description text if the URL
# requires login (e.g. Indeed app-tracker links, LinkedIn without account).
INTAKE_HEADERS = [
    "Job URL",        # A — paste job URL here
    "Description",    # B — optional: paste full JD text here if URL won't load
    "Status",         # C — auto-filled
    "Company",        # D — auto-filled
    "Job Title",      # E — auto-filled
    "Score %",        # F — auto-filled
    "Match Reasons",  # G — auto-filled
    "Skill Gaps",     # H — auto-filled
    "Location",       # I — auto-filled
    "Salary",         # J — auto-filled
    "Airtable ID",    # K — auto-filled
    "Notes",          # L — auto-filled
    "Date Added",     # M — auto-filled
]
IC_URL     = 0; IC_DESC    = 1; IC_STATUS  = 2; IC_COMPANY = 3
IC_TITLE   = 4; IC_SCORE   = 5; IC_MATCH   = 6; IC_GAPS    = 7
IC_LOC     = 8; IC_SAL     = 9; IC_ATID    = 10; IC_NOTES  = 11
IC_DATE    = 12

INTAKE_STATUS_PENDING      = "Pending"
INTAKE_STATUS_PROCESSING   = "Processing…"
INTAKE_STATUS_DONE         = "Done"
INTAKE_STATUS_LOW_SCORE    = "Low Score"
INTAKE_STATUS_ERROR        = "Error"
INTAKE_STATUS_NEED_DESC    = "Need Description"  # URL blocked — paste JD in col B

MIN_SCORE = 0.65  # minimum match score to generate + push

# ── Applications tab column layout (existing) ─────────────────────────────────
APPS_HEADERS = [
    "Company", "Job Title", "Score %", "Location", "Salary",
    "Platform", "Match Reasons", "Skill Gaps", "Apply URL",
    "Status", "Apply?"
]
C_COMPANY  = 0; C_TITLE = 1; C_SCORE = 2; C_LOCATION = 3; C_SALARY = 4
C_PLATFORM = 5; C_MATCH = 6; C_GAPS  = 7; C_URL      = 8
C_STATUS   = 9; C_APPLY = 10

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import Flow
except ImportError:
    print("ERROR: Google libraries not installed.")
    print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)


# ── WSL-safe OAuth ─────────────────────────────────────────────────────────────
def _local_server_oauth(creds_file, scopes):
    import http.server, urllib.parse

    with open(creds_file) as f:
        client_config = json.load(f)

    redirect_uri = f"http://localhost:{OAUTH_PORT}"
    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print(f"\n{'='*60}")
    print(f"  GOOGLE SHEETS AUTH")
    print(f"  Sign in as: benjaminwanjiku25@gmail.com")
    print(f"{'='*60}")
    print(f"\n  Open this URL in Windows Chrome/Edge:\n")
    print(f"  {auth_url}\n")
    print(f"{'='*60}")
    print(f"  Waiting for callback on localhost:{OAUTH_PORT} ...")
    print(f"{'='*60}\n")

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            result["path"]  = self.path
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["error"] = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2 style='font-family:sans-serif;color:green'>"
                b"&#10003; Auth complete! You can close this tab.</h2>"
            )

    server = http.server.HTTPServer(("0.0.0.0", OAUTH_PORT), Handler)
    server.handle_request()
    server.server_close()

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        sys.exit(1)

    import os as _os
    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    authorization_response = f"http://localhost:{OAUTH_PORT}{result['path']}"
    flow.fetch_token(authorization_response=authorization_response)
    print("  Auth successful!")
    return flow.credentials


def get_sheets_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                print(f"ERROR: credentials.json not found at {CREDS_FILE}")
                sys.exit(1)
            creds = _local_server_oauth(CREDS_FILE, SCOPES)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("sheets", "v4", credentials=creds)


# ── Spreadsheet management ─────────────────────────────────────────────────────
def load_sheet_id():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    return raw.get("profiles", {}).get("google:default", {}).get("sheets_id")


def load_or_create_sheet(service):
    """Return the spreadsheet ID, creating one if needed."""
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    sheet_id = raw.get("profiles", {}).get("google:default", {}).get("sheets_id")

    if not sheet_id:
        body = {
            "properties": {"title": SHEET_TITLE},
            "sheets": [{"properties": {"title": TAB_APPS, "index": 0}}]
        }
        resp = service.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        sheet_id = resp["spreadsheetId"]
        raw.setdefault("profiles", {}).setdefault("google:default", {})
        raw["profiles"]["google:default"]["sheets_id"] = sheet_id
        with open(AUTH_FILE, "w") as f:
            json.dump(raw, f, indent=2)
        print(f"  Created Google Sheet (ID saved to auth-profiles.json)")

    return sheet_id


def _get_tab_id(service, sheet_id, tab_name):
    """Return the numeric sheetId of a tab by name, or None."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == tab_name:
            return sh["properties"]["sheetId"]
    return None


def ensure_intake_tab(service, sheet_id):
    """Create/update the Job Intake tab with headers + formatting."""
    tab_id = _get_tab_id(service, sheet_id, TAB_INTAKE)

    if tab_id is None:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": TAB_INTAKE, "index": 1}}}]}
        ).execute()
        tab_id = _get_tab_id(service, sheet_id, TAB_INTAKE)
        print(f"  Created '{TAB_INTAKE}' tab")

    # Always rewrite headers (handles schema upgrades)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{TAB_INTAKE}!A1",
        valueInputOption="RAW",
        body={"values": [INTAKE_HEADERS]}
    ).execute()

    # Formatting
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"
            }},
            # Header row: dark blue + bold
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "foregroundColor": {"red":1,"green":1,"blue":1}},
                    "backgroundColor": {"red": 0.17, "green": 0.38, "blue": 0.65}
                }},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"
            }},
            # URL column A — light yellow
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.80}
                }},
                "fields": "userEnteredFormat.backgroundColor"
            }},
            # Description column B — light blue (optional paste area)
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": 1, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.88, "green": 0.94, "blue": 1.0}
                }},
                "fields": "userEnteredFormat.backgroundColor"
            }},
            {"autoResizeDimensions": {
                "dimensions": {"sheetId": tab_id, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": len(INTAKE_HEADERS)}
            }},
        ]}
    ).execute()
    print(f"  '{TAB_INTAKE}' ready — col A: URL | col B: Description (optional)")
    return tab_id


# ── Cell update helpers ────────────────────────────────────────────────────────
def _sheet_row(service, sheet_id, tab, row_num, values):
    """Update a full row (list of values) at the given 1-based row number."""
    col_end = chr(ord("A") + len(values) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A{row_num}:{col_end}{row_num}",
        valueInputOption="RAW",
        body={"values": [values]}
    ).execute()


def _sheet_cell(service, sheet_id, tab, row_num, col_idx, value):
    """Update a single cell."""
    col_letter = chr(ord("A") + col_idx)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!{col_letter}{row_num}",
        valueInputOption="RAW",
        body={"values": [[value]]}
    ).execute()


# ── Job description scraper ────────────────────────────────────────────────────
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def _extract_jd_from_html(raw_html):
    """Extract job description text + page title from raw HTML string."""
    import gzip
    if isinstance(raw_html, bytes):
        try:    raw_html = gzip.decompress(raw_html).decode("utf-8", errors="replace")
        except: raw_html = raw_html.decode("utf-8", errors="replace")

    title_m  = re.search(r"<title[^>]*>([^<]+)</title>", raw_html, re.I)
    og_title = re.search(r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', raw_html, re.I)
    page_title = html.unescape((og_title.group(1) if og_title else
                                title_m.group(1) if title_m else "").strip())

    desc = ""
    for pattern in [
        r'id="jobDescriptionText"[^>]*>([\s\S]*?)</div>',
        r'data-testid="jobDescriptionText"[^>]*>([\s\S]*?)</div>',
        r'class="[^"]*jobsearch-jobDescriptionText[^"]*"[^>]*>([\s\S]*?)</div>',
        r'class="[^"]*description__text[^"]*"[^>]*>([\s\S]*?)</section>',
        r'class="[^"]*job-description[^"]*"[^>]*>([\s\S]*?)</div>',
        r'<article[^>]*>([\s\S]*?)</article>',
    ]:
        m = re.search(pattern, raw_html, re.I)
        if m:
            candidate = re.sub(r"<[^>]+>", " ", m.group(1))
            candidate = html.unescape(re.sub(r"\s+", " ", candidate).strip())
            if len(candidate) > 300:
                desc = candidate
                break

    if not desc:
        body = re.sub(r"<script[\s\S]*?</script>", " ", raw_html, flags=re.I)
        body = re.sub(r"<style[\s\S]*?</style>",  " ", body,     flags=re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        desc = html.unescape(re.sub(r"\s+", " ", body).strip())

    return desc[:4000], page_title


def _fetch_with_playwright(url):
    """Use Playwright/Chromium to fetch a JS-rendered page. Returns (desc, title)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", ""

    WSL2_ARGS = ["--disable-dev-shm-usage", "--no-sandbox",
                 "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=WSL2_ARGS)
            ctx  = browser.new_context(
                user_agent=_BROWSER_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 900},
            )
            # Load saved Indeed cookies if available
            SESSION_FILE = os.path.join(SKILL_DIR, "indeed_session.json")
            if os.path.exists(SESSION_FILE):
                try:
                    ctx.add_cookies(json.load(open(SESSION_FILE)))
                except Exception:
                    pass

            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(2)
            raw_html = page.content()
            browser.close()
        return _extract_jd_from_html(raw_html)
    except Exception as e:
        err = str(e)
        if "libnspr4" in err or "shared libraries" in err or "TargetClosedError" in err:
            raise RuntimeError(
                "Playwright/Chromium missing system libraries. "
                "Fix: sudo apt-get install -y libnspr4 libnss3 libasound2t64"
            )
        return "", ""


def _normalise_url(url):
    """
    Strip tracking/auth params from known job board URLs so they're publicly fetchable.
    Indeed: keep only ?jk=  (strips from=, hl=, tk= etc.)
    LinkedIn: strip tracking params, use /view/ path only.
    """
    import urllib.parse as _up
    parsed = _up.urlparse(url)

    if "indeed.com" in parsed.netloc:
        qs = _up.parse_qs(parsed.query)
        jk = qs.get("jk", [""])[0]
        if jk:
            return f"https://www.indeed.com/viewjob?jk={jk}"

    if "linkedin.com" in parsed.netloc and "/jobs/view/" in parsed.path:
        # Strip all query params from LinkedIn job view URLs
        return _up.urlunparse(parsed._replace(query="", fragment=""))

    return url


def fetch_jd_from_url(url):
    """
    Fetch raw job description text from a URL.
    Strategy:
      1. Normalise URL (strip auth/tracking params that force login)
      2. Try urllib (works for most public career pages)
      3. If blocked (401/403) or empty, try Playwright (needs chromium system libs)
      4. If Playwright unavailable, raise descriptive error so caller can set
         INTAKE_STATUS_NEED_DESC and prompt user to paste description manually.
    Returns (desc_text, page_title).
    """
    url = _normalise_url(url)

    # ── urllib attempt ────────────────────────────────────────────────────────
    urllib_err = None
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            raw_html = r.read()
        desc, title = _extract_jd_from_html(raw_html)
        if len(desc) >= 300:
            return desc, title
        # Got HTML but couldn't extract description — try Playwright
    except urllib.error.HTTPError as e:
        urllib_err = f"HTTP {e.code}"
    except Exception as e:
        urllib_err = str(e)

    # ── Playwright fallback ───────────────────────────────────────────────────
    try:
        desc, title = _fetch_with_playwright(url)
        if len(desc) >= 300:
            return desc, title
    except RuntimeError as e:
        # Missing system libs — re-raise so caller can give helpful message
        raise RuntimeError(str(e)) from None
    except Exception:
        pass

    # ── Nothing worked ────────────────────────────────────────────────────────
    hint = ""
    if "indeed.com" in url:
        hint = " (Indeed requires login — paste the job description into column B)"
    elif "linkedin.com" in url:
        hint = " (LinkedIn requires login — paste the job description into column B)"
    else:
        hint = " — paste the job description into column B"

    raise ValueError(f"Could not fetch job description from URL{hint}. "
                     f"{'urllib error: ' + urllib_err if urllib_err else ''}")


# ── GPT: extract company/title + score from raw text ─────────────────────────
def gpt_extract_and_score(openai_key, jd_text, profile):
    """
    Single GPT call: extract company name, job title, score, match reasons,
    skill gaps, location, salary from raw job description text.
    Returns dict.
    """
    p = profile
    per = p["personal"]
    exp = p["experience"]
    sk  = p["skills"]

    profile_summary = (
        f"Name: {per['full_name']}\n"
        f"Current Title: {exp.get('current_title', '')}\n"
        f"Years of Experience: {exp.get('years_total', 3)}\n"
        f"Skills: {', '.join(sk.get('programming_languages', []) + sk.get('frameworks', []) + sk.get('tools', []))}\n"
        f"Certifications: {', '.join(sk.get('certifications', []))}\n"
        f"Location: {per.get('location', {}).get('city', 'Seattle')}, USA\n"
        f"Work Auth: Authorized, no sponsorship needed"
    )

    data = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": f"""Extract information from this job posting and score the match.

=== CANDIDATE ===
{profile_summary}

=== JOB POSTING TEXT ===
{jd_text[:3000]}

Return ONLY valid JSON (no markdown):
{{
  "company": "<company name>",
  "title": "<job title>",
  "location": "<city, state or Remote>",
  "salary": "<salary range or Not listed>",
  "score": <0.0-1.0 match score>,
  "match_reasons": ["<reason1>", "<reason2>"],
  "gaps": ["<gap1>"]
}}"""}],
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {openai_key}",
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"].strip()
    return json.loads(raw)


# ── Airtable helpers ──────────────────────────────────────────────────────────
def _at_auth():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    at = raw["profiles"]["airtable:default"]
    return at["key"], at["base_id"], at["table_id"]


def _at_create(key, base_id, table_id, fields):
    url  = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    body = json.dumps({"fields": fields}).encode()
    req  = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _at_url_exists(key, base_id, table_id, url):
    """Return True if a record with this apply URL already exists."""
    import urllib.parse
    encoded = urllib.parse.quote(f'{{Apply URL}}="{url}"')
    api_url = f"https://api.airtable.com/v0/{base_id}/{table_id}?filterByFormula={encoded}&fields%5B%5D=Apply+URL&pageSize=1"
    req = urllib.request.Request(api_url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        return len(d.get("records", [])) > 0
    except Exception:
        return False


# ── Core intake processor ─────────────────────────────────────────────────────
def process_intake_row(service, sheet_id, row_num, url, description, openai_key, profile):
    """
    Full pipeline for one intake sheet row.
    - url: job posting URL (required)
    - description: pre-pasted JD text (optional — skips URL fetch if provided)
    row_num: 1-based sheet row number.
    """
    print(f"\n  Processing row {row_num}: {url[:70]}")
    _sheet_cell(service, sheet_id, TAB_INTAKE, row_num, IC_STATUS, INTAKE_STATUS_PROCESSING)

    jd_text    = ""
    page_title = ""

    try:
        # 1. Get job description ───────────────────────────────────────────────
        if description and len(description.strip()) >= 100:
            # User pasted description directly — use it
            jd_text    = description.strip()[:4000]
            page_title = ""
            print(f"    Using pasted description ({len(jd_text)} chars)")
        else:
            # Fetch from URL
            print(f"    Fetching job description from URL...")
            try:
                jd_text, page_title = fetch_jd_from_url(url)
            except ValueError as e:
                # URL blocked — ask user to paste description in col B
                note = str(e)
                print(f"    {note}")
                _sheet_row(service, sheet_id, TAB_INTAKE, row_num, [
                    url, "", INTAKE_STATUS_NEED_DESC,
                    "", "", "", "", "", "", "", "",
                    note, datetime.now().strftime("%Y-%m-%d %H:%M"),
                ])
                return
            except RuntimeError as e:
                # Playwright missing system libs
                note = str(e)
                print(f"    {note}")
                _sheet_row(service, sheet_id, TAB_INTAKE, row_num, [
                    url, "", INTAKE_STATUS_NEED_DESC,
                    "", "", "", "", "", "", "", "",
                    note, datetime.now().strftime("%Y-%m-%d %H:%M"),
                ])
                return

        # 2. Extract company/title + score ─────────────────────────────────────
        print(f"    Scoring with GPT...")
        extracted = gpt_extract_and_score(openai_key, jd_text, profile)
        company  = extracted.get("company", "Unknown")
        title    = extracted.get("title",   page_title or "Unknown")
        score    = float(extracted.get("score", 0))
        location = extracted.get("location", "")
        salary   = extracted.get("salary", "")
        match_r  = "; ".join(extracted.get("match_reasons", []))
        gaps     = "; ".join(extracted.get("gaps", []))

        print(f"    {company} — {title} — score={score:.0%}")

        if score < MIN_SCORE:
            _sheet_row(service, sheet_id, TAB_INTAKE, row_num, [
                url, description, f"{INTAKE_STATUS_LOW_SCORE} ({score:.0%})",
                company, title, f"{score:.0%}", match_r, gaps,
                location, salary, "", f"Score below {MIN_SCORE:.0%} threshold",
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ])
            print(f"    Skipped (score {score:.0%} < {MIN_SCORE:.0%})")
            return

        # 3. AI tailor resume + cover letter ───────────────────────────────────
        print(f"    AI tailoring resume + cover letter...")
        sys.path.insert(0, SKILL_DIR)
        from ai_tailoring import tailor_resume, write_cover_letter

        tailored  = tailor_resume(openai_key, profile, jd_text, company, title)
        cl_result = write_cover_letter(
            openai_key, profile, jd_text, company, title,
            tailored.get("summary", "")
        )

        # 4. Generate PDFs ─────────────────────────────────────────────────────
        print(f"    Generating PDFs...")
        from pdf_generator import generate_tailored_resume, generate_cover_letter
        os.makedirs(os.path.join(OUTPUT_BASE, TODAY, "pdfs"), exist_ok=True)

        generate_tailored_resume(tailored, company, title)
        generate_cover_letter(company, title, cl_result.get("cover_letter", ""))

        # 5. Push to Airtable ──────────────────────────────────────────────────
        print(f"    Pushing to Airtable...")
        at_key, at_base_id, at_table_id = _at_auth()
        record_id = ""
        if not _at_url_exists(at_key, at_base_id, at_table_id, url):
            resp = _at_create(at_key, at_base_id, at_table_id, {
                "Job Title":     title,
                "Company":       company,
                "Score":         round(score * 100),
                "Location":      location,
                "Salary":        salary,
                "Platform":      "Sheet Intake",
                "Match Reasons": match_r,
                "Skill Gaps":    gaps,
                "Apply URL":     url,
                "Cover Letter":  cl_result.get("cover_letter", ""),
                "Status":        "Ready to Apply",
            })
            record_id = resp.get("id", "")
            print(f"    Airtable record: {record_id}")
        else:
            print(f"    Airtable: URL already exists — skipped duplicate")

        # 6. Update sheet row → Done ───────────────────────────────────────────
        _sheet_row(service, sheet_id, TAB_INTAKE, row_num, [
            url, description, INTAKE_STATUS_DONE,
            company, title, f"{score:.0%}", match_r, gaps,
            location, salary, record_id, "",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ])
        print(f"    Done: {company} — {title} ({score:.0%})")

    except Exception as e:
        err_msg = str(e)[:160]
        print(f"    ERROR: {err_msg}")
        _sheet_row(service, sheet_id, TAB_INTAKE, row_num, [
            url, description, INTAKE_STATUS_ERROR,
            "", "", "", "", "", "", "", "", err_msg,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ])


# ── Watch loop ────────────────────────────────────────────────────────────────
def watch_intake(service, sheet_id, interval=60):
    """
    Poll the Job Intake tab every `interval` seconds.
    Processes rows where URL is set and Status is blank, Pending, or Need Description
    (the last case means description was since pasted into col B).
    """
    ensure_intake_tab(service, sheet_id)

    with open(AUTH_FILE) as f:
        openai_key = json.load(f)["profiles"]["openai:default"]["key"]
    profile = json.load(open(PROFILE_PATH))["profile"]

    ncols    = len(INTAKE_HEADERS)
    col_end  = chr(ord("A") + ncols - 1)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    print(f"\n{'='*60}", flush=True)
    print(f"  INTAKE WATCHER — polling every {interval}s", flush=True)
    print(f"  Sheet: {sheet_url}", flush=True)
    print(f"  col A = Job URL  |  col B = Description (paste if URL blocked)", flush=True)
    print(f"  Min score: {MIN_SCORE:.0%}  |  Ctrl-C to stop", flush=True)
    print(f"{'='*60}", flush=True)

    while True:
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"{TAB_INTAKE}!A:{col_end}"
            ).execute()
            rows = result.get("values", [])

            for i, row in enumerate(rows[1:], start=2):
                padded = row + [""] * (ncols - len(row))
                url    = padded[IC_URL].strip()
                desc   = padded[IC_DESC].strip()
                status = padded[IC_STATUS].strip()

                # Process if: URL present AND (no status yet, or was "Need Description"
                # and now has a description pasted in col B)
                should_process = url and (
                    status in ("", INTAKE_STATUS_PENDING) or
                    (status == INTAKE_STATUS_NEED_DESC and desc)
                )

                if should_process:
                    process_intake_row(service, sheet_id, i, url, desc, openai_key, profile)
                    time.sleep(2)

        except KeyboardInterrupt:
            print("\n  Watcher stopped.", flush=True)
            break
        except Exception as e:
            print(f"  Watch error: {e}", flush=True)

        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Watching…", flush=True)
        time.sleep(interval)


# ── Add URL to intake tab ──────────────────────────────────────────────────────
def add_url_to_intake(service, sheet_id, url, description=""):
    """Append a new URL row to the intake tab (Status = Pending)."""
    ensure_intake_tab(service, sheet_id)

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB_INTAKE}!A:A"
    ).execute()
    next_row = len(result.get("values", [])) + 1

    _sheet_row(service, sheet_id, TAB_INTAKE, next_row, [
        url, description, INTAKE_STATUS_PENDING,
        "", "", "", "", "", "", "", "", "",
        datetime.now().strftime("%Y-%m-%d %H:%M"),
    ])
    print(f"  Added row {next_row}: {url}")
    return next_row


# ── Applications tab: format ───────────────────────────────────────────────────
def format_apps_sheet(service, sheet_id):
    tab_id = _get_tab_id(service, sheet_id, TAB_APPS) or 0

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"
            }},
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.27, "green": 0.27, "blue": 0.27}
                }},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"
            }},
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": C_APPLY, "endColumnIndex": C_APPLY + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.85, "green": 0.96, "blue": 0.85}
                }},
                "fields": "userEnteredFormat.backgroundColor"
            }},
            {"autoResizeDimensions": {
                "dimensions": {"sheetId": tab_id, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": len(APPS_HEADERS)}
            }},
        ]}
    ).execute()


# ── Push: pipeline JSON → Applications tab ────────────────────────────────────
def find_today_json():
    candidates = [
        os.path.join(OUTPUT_BASE, f"{TODAY}_matches.json"),
        os.path.join(OUTPUT_BASE, TODAY, "matches.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    files = sorted(
        [f for f in os.listdir(OUTPUT_BASE) if f.endswith("_matches.json")],
        reverse=True
    )
    if files:
        return os.path.join(OUTPUT_BASE, files[0])
    return None


def push_to_sheet(service, sheet_id, jobs):
    rows = [APPS_HEADERS]
    for job in jobs:
        rows.append([
            job.get("company", ""),
            job.get("title", ""),
            f"{int(round(job.get('score', 0) * 100))}%",
            job.get("location", ""),
            job.get("salary", ""),
            job.get("platform", ""),
            job.get("match_reasons", ""),
            job.get("skill_gaps", ""),
            job.get("apply_url", ""),
            "Pending Review",
            "",
        ])

    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{TAB_APPS}!A:K"
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{TAB_APPS}!A1",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()

    format_apps_sheet(service, sheet_id)
    return len(jobs)


# ── Pull: YES rows → PDFs + Airtable ──────────────────────────────────────────
def pull_yes_rows(service, sheet_id):
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB_APPS}!A:K"
    ).execute()
    rows = result.get("values", [])
    yes_jobs = []
    for row in rows[1:]:
        padded = row + [""] * (max(0, len(APPS_HEADERS) - len(row)))
        flag = (padded[C_APPLY] if len(padded) > C_APPLY else "").strip().upper()
        if flag in ("YES", "Y", "APPLY"):
            yes_jobs.append({
                "company":       padded[C_COMPANY],
                "title":         padded[C_TITLE],
                "score":         float((padded[C_SCORE]).replace("%", "") or 0) / 100,
                "location":      padded[C_LOCATION],
                "salary":        padded[C_SALARY],
                "platform":      padded[C_PLATFORM],
                "match_reasons": padded[C_MATCH],
                "skill_gaps":    padded[C_GAPS],
                "apply_url":     padded[C_URL],
            })
    return yes_jobs


def process_yes_jobs(yes_jobs, json_path):
    if not yes_jobs:
        print("  No YES rows found in sheet.")
        return

    # Enrich with cover letters from JSON
    url_to_cl = {}
    if json_path and os.path.exists(json_path):
        for entry in json.load(open(json_path)):
            url = entry.get("apply_url", "")
            cl  = entry.get("cover_letter", "")
            if url and cl:
                url_to_cl[url] = cl

    for job in yes_jobs:
        job["cover_letter"] = url_to_cl.get(job["apply_url"], "")

    # Generate PDFs
    sys.path.insert(0, SKILL_DIR)
    from pdf_generator import generate_resume, generate_cover_letter
    resume_path = generate_resume()
    for job in yes_jobs:
        cl_text = job.get("cover_letter", "")
        if cl_text:
            job["cover_pdf"]  = generate_cover_letter(job["company"], job["title"], cl_text)
            job["resume_pdf"] = resume_path

    # Push to Airtable
    from airtable_sync import (
        load_auth as at_load_auth,
        ensure_base,
        fetch_existing_urls,
        batch_create_records,
        AT_BASE_URL, TABLE_NAME,
    )
    import urllib.parse

    at_key, at_base_id, at_table_id = at_load_auth()
    at_base_id, at_table_id = ensure_base(at_key, at_base_id, at_table_id)
    existing_urls = fetch_existing_urls(at_key, at_base_id, at_table_id)
    created_pairs = batch_create_records(at_key, at_base_id, yes_jobs, existing_urls)

    for _, record_id in created_pairs:
        url  = f"{AT_BASE_URL}/{at_base_id}/{urllib.parse.quote(TABLE_NAME)}/{record_id}"
        body = json.dumps({"fields": {"Status": "Ready to Apply"}}).encode()
        req  = urllib.request.Request(url, data=body, method="PATCH",
            headers={"Authorization": f"Bearer {at_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30): pass
        time.sleep(0.22)

    print(f"  Pushed {len(created_pairs)} records to Airtable (status: Ready to Apply)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    mode_watch  = "--watch"   in sys.argv
    mode_add    = "--add"     in sys.argv
    mode_push   = "--push"    in sys.argv
    mode_pull   = "--pull"    in sys.argv
    mode_url    = "--url"     in sys.argv

    if not any([mode_watch, mode_add, mode_push, mode_pull, mode_url]):
        print(__doc__)
        sys.exit(0)

    service  = get_sheets_service()
    sheet_id = load_or_create_sheet(service)
    url_str  = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    if mode_url:
        print(f"  Sheet: {url_str}")
        return

    if mode_watch:
        watch_intake(service, sheet_id)
        return

    if mode_add:
        idx = sys.argv.index("--add")
        job_url = sys.argv[idx + 1]
        ensure_intake_tab(service, sheet_id)
        add_url_to_intake(service, sheet_id, job_url)
        print(f"  Sheet: {url_str}")
        return

    print(f"\n{'='*60}")
    print(f"  SHEETS SYNC — {TODAY}")
    print(f"  Sheet: {url_str}")
    print(f"{'='*60}")

    if mode_push:
        if "--json" in sys.argv:
            idx       = sys.argv.index("--json")
            json_path = sys.argv[idx + 1]
        else:
            json_path = find_today_json()

        if not json_path or not os.path.exists(json_path):
            print("  ERROR: No matches JSON found. Run job_pipeline.py first.")
            sys.exit(1)

        print(f"\n  Loading jobs from: {json_path}")
        jobs = []
        for entry in json.load(open(json_path)):
            jobs.append({
                "company":       entry.get("company", ""),
                "title":         entry.get("title", ""),
                "score":         entry.get("score", 0),
                "location":      entry.get("location", ""),
                "salary":        entry.get("salary", ""),
                "platform":      entry.get("platform", ""),
                "match_reasons": "; ".join(entry.get("scoring", {}).get("match_reasons", [])),
                "skill_gaps":    "; ".join(entry.get("scoring", {}).get("gaps", [])),
                "apply_url":     entry.get("apply_url", ""),
            })
        count = push_to_sheet(service, sheet_id, jobs)
        print(f"\n  Pushed {count} jobs → mark 'Apply?' column YES then run --pull")

    if mode_pull:
        json_path = None
        if "--json" in sys.argv:
            idx       = sys.argv.index("--json")
            json_path = sys.argv[idx + 1]
        else:
            json_path = find_today_json()

        yes_jobs = pull_yes_rows(service, sheet_id)
        print(f"\n  Found {len(yes_jobs)} YES rows")
        if yes_jobs:
            process_yes_jobs(yes_jobs, json_path)

    print(f"\n{'='*60}")
    print(f"  Done")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        svc = get_sheets_service()
        sid = load_or_create_sheet(svc)
        ensure_intake_tab(svc, sid)
        print(f"  Setup complete.")
        print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sid}")
    else:
        main()
