#!/usr/bin/env python3
"""
Google Drive PDF Uploader — Benjamin's Job Application Pipeline
Uploads resume + cover letter PDFs to a public Google Drive folder,
then patches Airtable records with the real attachment URLs.

Usage:
    python drive_uploader.py              # upload today's PDFs + patch Airtable
    python drive_uploader.py --setup      # OAuth only
    python drive_uploader.py --date 2026-03-02  # specific date

Setup (one-time):
    1. In Google Cloud Console → enable BOTH Gmail API AND Drive API
    2. credentials.json is already at:
       ~/.openclaw/workspace/skills/job-auto-apply/credentials.json
    3. Run: python drive_uploader.py --setup
       (opens browser once for Gmail + Drive consent)
"""

import json, os, sys, time, re
from datetime import datetime

SKILL_DIR  = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply")
CREDS_FILE = os.path.join(SKILL_DIR, "credentials.json")
TOKEN_FILE = os.path.join(SKILL_DIR, "drive_token.json")
AUTH_FILE  = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")

TODAY = datetime.now().strftime("%Y-%m-%d")

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    print("ERROR: Google libraries not installed.")
    print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

# Drive file scope only — safer than full drive access
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# ── Airtable helpers (inline to avoid circular import issues) ─────────────────
def load_at_auth():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    at = raw["profiles"]["airtable:default"]
    return at["key"], at["base_id"], at["table_id"]


def at_patch(key, base_id, table_id, record_id, fields):
    import urllib.request, urllib.parse
    url     = f"https://api.airtable.com/v0/{base_id}/{table_id}/{record_id}"
    payload = json.dumps({"fields": fields}).encode()
    req     = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_all_records(key, base_id, table_id):
    import urllib.request
    records, offset = [], None
    while True:
        url = (f"https://api.airtable.com/v0/{base_id}/{table_id}"
               f"?pageSize=100" + (f"&offset={offset}" if offset else ""))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        records.extend(d.get("records", []))
        offset = d.get("offset")
        if not offset:
            break
    return records


# ── Manual OAuth flow (WSL-safe: no browser needed) ──────────────────────────
OAUTH_PORT = 8888


def _local_server_oauth(creds_file, scopes):
    """
    Generates the auth URL, starts a one-shot HTTP server on port 8888,
    and waits for Google's redirect callback. WSL2 forwards localhost:8888
    to Windows so opening the URL in Chrome completes auth automatically.
    """
    import threading
    import http.server
    import urllib.parse
    from google_auth_oauthlib.flow import Flow

    with open(creds_file) as f:
        client_config = json.load(f)

    redirect_uri = f"http://localhost:{OAUTH_PORT}"
    flow = Flow.from_client_config(client_config, scopes=scopes,
                                   redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print("\n" + "="*60)
    print("  GOOGLE DRIVE AUTH")
    print("  Sign in as: benjaminwanjiku25@gmail.com")
    print("="*60)
    print(f"\n  Open this URL in Windows Chrome/Edge:\n")
    print(f"  {auth_url}\n")
    print("="*60)
    print(f"  Waiting for callback on localhost:{OAUTH_PORT} ...")
    print("="*60 + "\n")

    # One-shot callback server
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # suppress access logs
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
    server.handle_request()  # blocks until one request arrives
    server.server_close()

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        sys.exit(1)
    if not result.get("path"):
        print("  ERROR: No callback received")
        sys.exit(1)

    import os as _os
    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    authorization_response = f"http://localhost:{OAUTH_PORT}{result['path']}"
    flow.fetch_token(authorization_response=authorization_response)
    print("  ✓ Auth successful!")
    return flow.credentials


# ── Google Drive auth ─────────────────────────────────────────────────────────
def get_drive_service():
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
        print(f"  ✓ Drive token saved to {TOKEN_FILE}")

    return build("drive", "v3", credentials=creds)


# ── Drive folder + upload ─────────────────────────────────────────────────────
def get_or_create_folder(service, name, parent_id=None):
    """Find or create a Drive folder by name."""
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    results = service.files().list(q=q, fields="files(id,name)").execute()
    files   = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def make_public(service, file_id):
    """Grant anyone-with-link read access."""
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        fields="id"
    ).execute()


def upload_file(service, local_path, filename, folder_id):
    """Upload a PDF to Drive, return its file ID."""
    # Check if file already exists in folder
    q = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=q, fields="files(id,name)").execute()
    if results.get("files"):
        return results["files"][0]["id"]  # already uploaded

    metadata = {"name": filename, "parents": [folder_id]}
    media    = MediaFileUpload(local_path, mimetype="application/pdf", resumable=False)
    file     = service.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()
    return file["id"]


def drive_url(file_id):
    """Direct download URL that works as an Airtable attachment."""
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    date = TODAY
    if "--date" in sys.argv:
        idx  = sys.argv.index("--date")
        date = sys.argv[idx + 1]

    pdf_dir = os.path.expanduser(f"~/job_applications/{date}/pdfs")
    if not os.path.exists(pdf_dir):
        print(f"ERROR: No PDFs found at {pdf_dir}")
        print(f"Run pdf_generator.py first.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  DRIVE UPLOADER — {date}")
    print(f"{'='*60}")

    # Auth
    print("\n  Authenticating Google Drive...")
    service = get_drive_service()
    print("  ✓ Drive connected")

    # Create folder structure: Job Applications > {date} > PDFs
    print("\n  Creating Drive folder structure...")
    root_id = get_or_create_folder(service, "Job Applications")
    date_id = get_or_create_folder(service, date, parent_id=root_id)
    pdfs_id = get_or_create_folder(service, "pdfs", parent_id=date_id)

    # Make the pdfs folder public (files inherit sharing)
    make_public(service, pdfs_id)
    print(f"  ✓ Public folder: Job Applications/{date}/pdfs")
    print(f"    https://drive.google.com/drive/folders/{pdfs_id}")

    # Collect all PDFs
    pdf_files = sorted([f for f in os.listdir(pdf_dir) if f.endswith(".pdf")])
    print(f"\n  Uploading {len(pdf_files)} PDFs...")

    file_id_map = {}  # filename → drive file_id
    for i, fname in enumerate(pdf_files, 1):
        local_path = os.path.join(pdf_dir, fname)
        fid        = upload_file(service, local_path, fname, pdfs_id)
        make_public(service, fid)
        file_id_map[fname] = fid
        print(f"  [{i:3}/{len(pdf_files)}] ✓ {fname}")

    print(f"\n  ✓ All PDFs uploaded to Drive")

    # ── Patch Airtable records ────────────────────────────────────────────────
    print(f"\n  Patching Airtable records with Drive URLs...")
    at_key, at_base_id, at_table_id = load_at_auth()
    records = fetch_all_records(at_key, at_base_id, at_table_id)
    print(f"  Found {len(records)} records")

    generic_resume_fid = file_id_map.get("resume.pdf")
    if not generic_resume_fid:
        print("  ⚠ resume.pdf (generic) not found — will use per-job tailored resumes only")

    patched  = 0
    skipped  = 0

    for rec in records:
        fields    = rec.get("fields", {})
        record_id = rec["id"]
        company   = fields.get("Company", "")
        title     = fields.get("Job Title", "")

        # Slug helper (matches pdf_generator.safe())
        def safe(s):
            return re.sub(r"[^\w\-]", "_", s).strip("_")[:50]

        co_slug = safe(company)
        ti_slug = safe(title)

        # ── Match tailored resume PDF (per-job, v2 pipeline) ─────────────────
        tailored_resume_fname = f"{co_slug}_{ti_slug}_resume.pdf"
        tailored_resume_fid   = file_id_map.get(tailored_resume_fname)
        if not tailored_resume_fid:
            # Fuzzy match by company slug
            for fname, fid in file_id_map.items():
                if fname.lower().startswith(co_slug[:20].lower()) and fname.endswith("_resume.pdf"):
                    tailored_resume_fid = fid
                    break

        # Use tailored if available, else fall back to generic resume
        resume_fid   = tailored_resume_fid or generic_resume_fid
        resume_fname = tailored_resume_fname if tailored_resume_fid else "resume.pdf"

        # ── Match cover letter PDF ────────────────────────────────────────────
        cover_fname = f"{co_slug}_{ti_slug}_cover.pdf"
        cover_fid   = file_id_map.get(cover_fname)
        if not cover_fid:
            for fname, fid in file_id_map.items():
                if fname.lower().startswith(co_slug[:20].lower()) and fname.endswith("_cover.pdf"):
                    cover_fid = fid
                    break

        fields_update = {}
        if resume_fid:
            fields_update["Resume PDF"] = [{"url": drive_url(resume_fid), "filename": resume_fname}]
        if cover_fid:
            fields_update["Cover Letter PDF"] = [{"url": drive_url(cover_fid), "filename": cover_fname}]

        if not fields_update:
            skipped += 1
            continue

        at_patch(at_key, at_base_id, at_table_id, record_id, fields_update)
        patched += 1
        resume_tag = "✓ Tailored" if tailored_resume_fid else ("✓ Resume" if resume_fid else "      ")
        print(f"  ✓ {company[:35]:35} {resume_tag:<12} {'✓ Cover' if cover_fid else '      '}")
        time.sleep(0.22)

    print(f"\n{'='*60}")
    print(f"  Upload complete!")
    print(f"  PDFs uploaded: {len(pdf_files)}")
    print(f"  Records patched: {patched}")
    print(f"  Records skipped: {skipped} (no matching cover PDF)")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{pdfs_id}")
    print(f"  Airtable: https://airtable.com/{at_base_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        print("Running Google Drive OAuth setup...")
        svc     = get_drive_service()
        about   = svc.about().get(fields="user").execute()
        print(f"  ✓ Connected as: {about['user']['emailAddress']}")
        print("  Setup complete. Run python drive_uploader.py to upload PDFs.")
    else:
        main()
