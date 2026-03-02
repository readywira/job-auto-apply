#!/usr/bin/env python3
"""
Gmail Response Tracker — Benjamin's Job Application Pipeline
Polls Gmail for responses from companies where status = "Submitted",
classifies them (Interview / Rejection / Follow-up / Offer),
and updates Airtable accordingly.

Usage:
    python gmail_tracker.py           # scan all submitted records
    python gmail_tracker.py --days 7  # look back 7 days (default: 30)
    python gmail_tracker.py --setup   # OAuth consent flow only

Setup (one-time):
    1. Create Google Cloud project → enable Gmail API
    2. Create OAuth 2.0 Desktop credentials → download credentials.json
    3. Place at: ~/.openclaw/workspace/skills/job-auto-apply/credentials.json
    4. Run: python gmail_tracker.py --setup
       (opens browser for consent → saves token.json)

Crontab (daily 9AM):
    0 9 * * * /path/to/job_venv/bin/python3 /path/to/gmail_tracker.py >> ~/job_applications/gmail_tracker.log 2>&1
"""

import json, os, sys, re, time
from datetime import datetime, timedelta

SKILL_DIR    = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply")
CREDS_FILE   = os.path.join(SKILL_DIR, "credentials.json")
TOKEN_FILE   = os.path.join(SKILL_DIR, "token.json")
AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")

# Import Airtable helpers
sys.path.insert(0, SKILL_DIR)
from airtable_sync import (
    load_auth as at_load_auth,
    fetch_records_by_status,
    update_status,
    at_get,
    at_patch,
    AT_BASE_URL,
    TABLE_NAME,
)
import urllib.parse

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
except ImportError:
    print("ERROR: Google libraries not installed.")
    print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Email classification keywords
INTERVIEW_KW  = ["interview", "schedule", "call", "meeting", "zoom", "teams", "phone screen",
                  "next steps", "move forward", "shortlisted"]
OFFER_KW      = ["offer", "congratulations", "pleased to offer", "we'd like to offer",
                  "job offer", "employment offer"]
REJECTION_KW  = ["unfortunately", "not moving forward", "decided to move", "not selected",
                  "other candidates", "not a fit", "position has been filled",
                  "not proceed", "won't be moving"]
FOLLOWUP_KW   = ["following up", "checking in", "status update", "application received",
                  "thank you for applying", "under review", "reviewing your application"]


# ── Gmail auth ────────────────────────────────────────────────────────────────
def _manual_oauth(creds_file, scopes, token_file):
    """WSL-safe OAuth: prints URL, user pastes back the redirect URL with ?code=."""
    import urllib.parse
    from google_auth_oauthlib.flow import Flow

    with open(creds_file) as f:
        client_config = json.load(f)

    flow = Flow.from_client_config(
        client_config, scopes=scopes, redirect_uri="http://localhost"
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print("\n" + "="*60)
    print("  GOOGLE AUTH — open this URL in your Windows browser:")
    print("  Sign in as: benjaminwanjiku25@gmail.com")
    print("="*60)
    print(f"\n{auth_url}\n")
    print("="*60)
    print("  After approving, copy the FULL URL from the address bar")
    print("  (browser shows 'connection refused' — that's fine)")
    print("="*60)

    raw  = input("\n  Paste the full redirect URL here: ").strip()
    code = None
    if "code=" in raw:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        code   = params.get("code", [None])[0]
    else:
        code = raw

    if not code:
        print("ERROR: could not extract auth code")
        sys.exit(1)

    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(token_file, "w") as f:
        f.write(creds.to_json())
    print(f"  ✓ Token saved to {token_file}")
    return creds


def get_gmail_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                print(f"ERROR: credentials.json not found at {CREDS_FILE}")
                print(f"  Place credentials.json at: {CREDS_FILE}")
                sys.exit(1)
            creds = _manual_oauth(CREDS_FILE, SCOPES, TOKEN_FILE)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Email classification ──────────────────────────────────────────────────────
def classify_email(subject, snippet):
    text = (subject + " " + snippet).lower()

    # Offer takes priority
    if any(kw in text for kw in OFFER_KW):
        return "Offer"
    if any(kw in text for kw in INTERVIEW_KW):
        return "Interview Scheduled"
    if any(kw in text for kw in REJECTION_KW):
        return "Rejected"
    if any(kw in text for kw in FOLLOWUP_KW):
        return "Follow-up"
    return None


# ── Extract company domain from job record ────────────────────────────────────
def guess_domain(company, apply_url):
    """Guess company email domain from apply URL or company name."""
    domains = set()

    # From URL
    if apply_url:
        m = re.search(r"https?://(?:www\.)?([^/]+)", apply_url)
        if m:
            host = m.group(1).lower()
            # Skip job board domains
            skip = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
                    "lever.co", "greenhouse.io", "workday.com", "icims.com", "jobvite.com",
                    "smartrecruiters.com", "bamboohr.com", "myworkdayjobs.com"}
            if not any(s in host for s in skip):
                domains.add(host)

    # From company name — simple slug
    if company:
        slug = re.sub(r"[^a-z0-9]", "", company.lower())
        if len(slug) > 2:
            domains.add(f"{slug}.com")

    return list(domains)


# ── Gmail search ─────────────────────────────────────────────────────────────
def search_emails(service, query, max_results=10):
    """Search Gmail and return list of message metadata."""
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    emails   = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in detail.get("payload", {}).get("headers", [])}
        emails.append({
            "id":      msg["id"],
            "from":    headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date":    headers.get("Date", ""),
            "snippet": detail.get("snippet", ""),
        })
    return emails


# ── Fetch all Airtable "Submitted" records ───────────────────────────────────
def fetch_submitted_records(at_key, at_base_id):
    return fetch_records_by_status(at_key, at_base_id, "Submitted")


# ── Process a single record ──────────────────────────────────────────────────
def check_record(service, record, at_key, at_base_id, days_back=30):
    fields    = record.get("fields", {})
    record_id = record["id"]
    company   = fields.get("Company", "")
    title     = fields.get("Job Title", "")
    apply_url = fields.get("Apply URL", "")

    domains = guess_domain(company, apply_url)
    if not domains:
        return None

    # Build Gmail query
    after_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    domain_q   = " OR ".join(f"from:{d}" for d in domains[:3])
    subject_q  = "subject:(interview OR application OR position OR offer OR opportunity OR role)"
    query      = f"({domain_q}) {subject_q} after:{after_date}"

    emails = search_emails(service, query, max_results=5)
    if not emails:
        return None

    # Classify each email, take strongest signal
    results = []
    for email in emails:
        classification = classify_email(email["subject"], email["snippet"])
        if classification:
            results.append((classification, email))

    if not results:
        return None

    # Priority: Offer > Interview > Rejection > Follow-up
    priority_order = ["Offer", "Interview Scheduled", "Rejected", "Follow-up"]
    results.sort(key=lambda x: priority_order.index(x[0])
                 if x[0] in priority_order else 99)

    best_class, best_email = results[0]

    return {
        "record_id":      record_id,
        "company":        company,
        "title":          title,
        "classification": best_class,
        "email_from":     best_email["from"],
        "email_subject":  best_email["subject"],
        "email_date":     best_email["date"],
        "email_snippet":  best_email["snippet"][:300],
    }


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    days_back = 30
    if "--days" in sys.argv:
        idx       = sys.argv.index("--days")
        days_back = int(sys.argv[idx + 1])

    print(f"\n{'='*60}")
    print(f"  GMAIL TRACKER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Looking back {days_back} days")
    print(f"{'='*60}")

    # OAuth setup
    print("\n  Authenticating Gmail...")
    service = get_gmail_service()
    print("  ✓ Gmail connected")

    # Airtable
    at_key, at_base_id, at_table_id = at_load_auth()
    if not at_base_id:
        print("ERROR: No Airtable base_id. Run airtable_sync.py --setup first.")
        sys.exit(1)

    # Fetch submitted jobs
    print("\n  Fetching submitted jobs from Airtable...")
    records = fetch_submitted_records(at_key, at_base_id)
    print(f"  Found {len(records)} submitted applications\n")

    if not records:
        print("  No submitted applications to check.")
        return

    found    = 0
    updated  = 0

    for rec in records:
        company = rec.get("fields", {}).get("Company", "?")
        title   = rec.get("fields", {}).get("Job Title", "?")
        print(f"  Checking: {company} — {title}...")

        result = check_record(service, rec, at_key, at_base_id, days_back)
        time.sleep(0.5)  # gentle Gmail rate limiting

        if not result:
            print(f"    → No relevant emails found")
            continue

        found += 1
        classification = result["classification"]
        print(f"    → {classification}: \"{result['email_subject'][:60]}\"")

        # Build Airtable update
        note = (
            f"[{datetime.now().strftime('%Y-%m-%d')}] {classification}\n"
            f"From: {result['email_from']}\n"
            f"Subject: {result['email_subject']}\n"
            f"Snippet: {result['email_snippet']}"
        )

        # Merge with existing notes
        existing_notes = rec.get("fields", {}).get("Notes", "")
        new_notes      = (existing_notes + "\n\n" + note).strip() if existing_notes else note

        update_status(
            at_key, at_base_id, result["record_id"],
            status=classification,
            notes=new_notes
        )
        updated += 1
        print(f"    ✓ Airtable status → {classification}")

    print(f"\n{'='*60}")
    print(f"  Checked:  {len(records)} submitted applications")
    print(f"  Found:    {found} email responses")
    print(f"  Updated:  {updated} Airtable records")
    print(f"  Done at:  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        print("Running Gmail OAuth setup...")
        svc = get_gmail_service()
        profile = svc.users().getProfile(userId="me").execute()
        print(f"  ✓ Connected as: {profile.get('emailAddress','?')}")
        print("  Setup complete. Run python gmail_tracker.py to start tracking.")
    else:
        main()
