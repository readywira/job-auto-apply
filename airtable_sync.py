#!/usr/bin/env python3
"""
Airtable Sync — Benjamin's Job Application Pipeline
Pushes scored job matches + PDFs to Airtable for review/approval.

Usage:
    python airtable_sync.py                         # sync today's match JSON
    python airtable_sync.py --json path/to/file.json
    python airtable_sync.py --setup                 # create base + table only

Reads auth from: ~/.openclaw/agents/main/agent/auth-profiles.json
  → profiles["airtable:default"]["key"]    — Personal Access Token (PAT)
  → profiles["airtable:default"]["base_id"] — auto-created and saved on first run
"""

import json, os, sys, urllib.request, urllib.parse, time
from datetime import datetime

AUTH_FILE    = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
OUTPUT_BASE  = os.path.expanduser("~/job_applications")
TODAY        = datetime.now().strftime("%Y-%m-%d")
SESSION_FILE = os.path.expanduser("~/.openclaw/workspace/skills/job-auto-apply/.airtable_state.json")

AT_BASE_URL  = "https://api.airtable.com/v0"
AT_META_URL  = "https://api.airtable.com/v0/meta"

TABLE_NAME   = "Job Applications"
BASE_NAME    = "Job Pipeline"

# Fields matching Airtable table schema
FIELD_DEFS = [
    {"name": "Job Title",        "type": "singleLineText"},
    {"name": "Company",          "type": "singleLineText"},
    {"name": "Score",            "type": "number",     "options": {"precision": 0}},
    {"name": "Location",         "type": "singleLineText"},
    {"name": "Salary",           "type": "singleLineText"},
    {"name": "Platform",         "type": "singleLineText"},
    {"name": "Match Reasons",    "type": "multilineText"},
    {"name": "Skill Gaps",       "type": "multilineText"},
    {"name": "Apply URL",        "type": "url"},
    {"name": "Cover Letter",     "type": "multilineText"},
    {"name": "Resume PDF",       "type": "multipleAttachments"},
    {"name": "Cover Letter PDF", "type": "multipleAttachments"},
    {
        "name": "Status",
        "type": "singleSelect",
        "options": {
            "choices": [
                {"name": "Pending Review",        "color": "yellowBright"},
                {"name": "Ready to Apply",        "color": "blueBright"},
                {"name": "Submitted",             "color": "greenBright"},
                {"name": "Rejected",              "color": "redBright"},
                {"name": "Interview Scheduled",   "color": "purpleBright"},
                {"name": "Offer",                 "color": "greenDark1"},
            ]
        }
    },
    {"name": "Applied Date",     "type": "date",       "options": {"dateFormat": {"name": "iso"}}},
    {"name": "Notes",            "type": "multilineText"},
]


# ── Auth helpers ─────────────────────────────────────────────────────────────
def load_auth():
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    profiles = raw.get("profiles", {})
    at = profiles.get("airtable:default", {})
    key = at.get("key", "")
    if not key:
        print("ERROR: No Airtable Personal Access Token found.")
        print("Add to auth-profiles.json: profiles['airtable:default']['key'] = 'patXXXX...'")
        sys.exit(1)
    return key, at.get("base_id", ""), at.get("table_id", "")


def save_base_id(base_id, table_id):
    with open(AUTH_FILE) as f:
        raw = json.load(f)
    raw.setdefault("profiles", {}).setdefault("airtable:default", {})
    raw["profiles"]["airtable:default"]["base_id"]  = base_id
    raw["profiles"]["airtable:default"]["table_id"] = table_id
    with open(AUTH_FILE, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"  ✓ Saved base_id={base_id}, table_id={table_id} to auth-profiles.json")


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def at_request(method, url, data=None, key=""):
    body = json.dumps(data).encode() if data is not None else None
    req  = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "Authorization":  f"Bearer {key}",
            "Content-Type":   "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code}: {body[:300]}")
        raise


def at_get(url, key):
    return at_request("GET", url, key=key)


def at_post(url, data, key):
    return at_request("POST", url, data, key)


def at_patch(url, data, key):
    return at_request("PATCH", url, data, key)


# ── Base / Table setup ───────────────────────────────────────────────────────
def create_table(key, base_id):
    """Create the Job Applications table inside an existing base."""
    url     = f"{AT_META_URL}/bases/{base_id}/tables"
    payload = {"name": TABLE_NAME, "fields": FIELD_DEFS}
    try:
        resp     = at_post(url, payload, key)
        table_id = resp["id"]
        print(f"  ✓ Created table '{TABLE_NAME}' ({table_id})")
        return table_id
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        # Table may already exist
        if "already exists" in body.lower() or e.code == 422:
            print(f"  Table '{TABLE_NAME}' already exists — finding its ID...")
            return find_table_id(key, base_id)
        raise


def find_table_id(key, base_id):
    """Look up the table ID by name within a base."""
    resp = at_get(f"{AT_META_URL}/bases/{base_id}/tables", key)
    for t in resp.get("tables", []):
        if t.get("name") == TABLE_NAME:
            return t["id"]
    return None


def ensure_base(key, base_id, table_id):
    """
    Ensure the base and table exist.
    Base must be created manually; we create/verify the table via API.
    """
    if not base_id:
        print("\n  ┌─────────────────────────────────────────────────────────┐")
        print("  │  ACTION REQUIRED: Create the Airtable base manually     │")
        print("  │                                                          │")
        print("  │  1. Go to airtable.com                                  │")
        print("  │  2. Click '+ Add a base' → 'Start from scratch'         │")
        print("  │  3. Name it: Job Pipeline                                │")
        print("  │  4. Copy the base ID from the URL (starts with 'app')   │")
        print("  │  5. Run: python airtable_sync.py --base appXXXXXXXXXX   │")
        print("  └─────────────────────────────────────────────────────────┘\n")
        sys.exit(1)

    # Verify base is accessible
    try:
        at_get(f"{AT_META_URL}/bases/{base_id}/tables", key)
        print(f"  ✓ Base accessible: {base_id}")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"  ERROR: PAT cannot access base {base_id}.")
            print("  Make sure the PAT has 'schema.tables:write' scope")
            print("  AND the base is included under 'Access' when creating the PAT.")
            sys.exit(1)
        raise

    # Create or locate the table
    if not table_id:
        table_id = find_table_id(key, base_id)
        if table_id:
            print(f"  ✓ Found existing table '{TABLE_NAME}' ({table_id})")
        else:
            print(f"  Creating table '{TABLE_NAME}'...")
            table_id = create_table(key, base_id)

    save_base_id(base_id, table_id)
    return base_id, table_id


# ── Deduplication ────────────────────────────────────────────────────────────
def fetch_existing_urls(key, base_id, table_id):
    """Return set of Apply URLs already in Airtable."""
    existing = set()
    offset   = None
    while True:
        url = (f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}"
               f"?fields%5B%5D=Apply+URL&pageSize=100"
               + (f"&offset={offset}" if offset else ""))
        resp   = at_get(url, key)
        for rec in resp.get("records", []):
            u = rec.get("fields", {}).get("Apply URL", "")
            if u:
                existing.add(u)
        offset = resp.get("offset")
        if not offset:
            break
    return existing



# ── Create Airtable records ───────────────────────────────────────────────────
def create_record(key, base_id, job_data):
    """Create a single record in the Job Applications table."""
    score_pct = int(round(job_data.get("score", 0) * 100))

    fields = {
        "Job Title":     job_data.get("title", ""),
        "Company":       job_data.get("company", ""),
        "Score":         score_pct,
        "Location":      job_data.get("location", ""),
        "Salary":        job_data.get("salary", ""),
        "Platform":      job_data.get("platform", ""),
        "Match Reasons": job_data.get("match_reasons", ""),
        "Skill Gaps":    job_data.get("skill_gaps", ""),
        "Apply URL":     job_data.get("apply_url", ""),
        "Cover Letter":  job_data.get("cover_letter", ""),
        "Status":        "Pending Review",
    }

    # Attach PDFs by URL if they're already hosted, or skip here (upload after)
    payload = {"fields": fields}
    url     = f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}"
    resp    = at_post(url, payload, key)
    return resp.get("id", "")


def batch_create_records(key, base_id, jobs, existing_urls):
    """Create up to 10 records at a time (Airtable limit)."""
    new_jobs = [j for j in jobs if j.get("apply_url") not in existing_urls]
    if not new_jobs:
        print("  All jobs already in Airtable — nothing to add.")
        return []

    print(f"  Syncing {len(new_jobs)} new jobs (skipping {len(jobs)-len(new_jobs)} duplicates)...")
    created = []
    BATCH   = 10

    for i in range(0, len(new_jobs), BATCH):
        batch   = new_jobs[i:i+BATCH]
        records = []
        for job in batch:
            score_pct = int(round(job.get("score", 0) * 100))
            records.append({
                "fields": {
                    "Job Title":     job.get("title", ""),
                    "Company":       job.get("company", ""),
                    "Score":         score_pct,
                    "Location":      job.get("location", ""),
                    "Salary":        job.get("salary", ""),
                    "Platform":      job.get("platform", ""),
                    "Match Reasons": job.get("match_reasons", ""),
                    "Skill Gaps":    job.get("skill_gaps", ""),
                    "Apply URL":     job.get("apply_url", ""),
                    "Cover Letter":  job.get("cover_letter", ""),
                    "Status":        "Pending Review",
                }
            })

        url  = f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}"
        resp = at_post(url, {"records": records}, key)
        batch_ids = [r["id"] for r in resp.get("records", [])]
        for j, rid in zip(batch, batch_ids):
            j["airtable_record_id"] = rid
        created.extend(zip(batch, batch_ids))
        print(f"    → Created records {i+1}–{i+len(batch_ids)} of {len(new_jobs)}")
        time.sleep(0.25)  # Airtable rate limit: 5 req/sec

    return created


# ── Store PDF paths in Notes (Airtable REST API cannot upload local files) ───
def upload_pdfs(key, base_id, table_id, created_pairs):
    """
    Airtable's REST API requires a public URL for attachments — local files
    cannot be uploaded directly. Instead, we write the Windows-accessible
    paths into the Notes field so PDFs are easy to locate.
    """
    for job, record_id in created_pairs:
        resume_path = job.get("resume_pdf", "")
        cover_path  = job.get("cover_pdf",  "")
        if not resume_path and not cover_path:
            continue

        # Convert WSL path → Windows path for display
        def to_win(p):
            return p.replace("/home/benji/", "C:\\Users\\admin\\").replace("/", "\\") if p else ""

        pdf_note = f"📄 Resume PDF: {to_win(resume_path)}\n📄 Cover PDF:  {to_win(cover_path)}"
        url      = f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}/{record_id}"
        at_patch(url, {"fields": {"Notes": pdf_note}}, key)
        time.sleep(0.22)


# ── Update record status ─────────────────────────────────────────────────────
def update_status(key, base_id, record_id, status, applied_date=None, notes=None):
    fields = {"Status": status}
    if applied_date:
        fields["Applied Date"] = applied_date
    if notes:
        fields["Notes"] = notes
    url  = f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}/{record_id}"
    at_patch(url, {"fields": fields}, key)


# ── Fetch records by status ──────────────────────────────────────────────────
def fetch_records_by_status(key, base_id, status="Ready to Apply"):
    records  = []
    offset   = None
    filter_q = urllib.parse.quote(f"{{Status}}='{status}'")
    while True:
        url = (f"{AT_BASE_URL}/{base_id}/{urllib.parse.quote(TABLE_NAME)}"
               f"?filterByFormula={filter_q}&pageSize=100"
               + (f"&offset={offset}" if offset else ""))
        resp   = at_get(url, key)
        records.extend(resp.get("records", []))
        offset  = resp.get("offset")
        if not offset:
            break
    return records


# ── Load today's match JSON (from pipeline) ──────────────────────────────────
def find_today_json():
    candidates = [
        os.path.join(OUTPUT_BASE, f"{TODAY}_matches.json"),
        os.path.join(OUTPUT_BASE, f"{TODAY}", "matches.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Try most recent
    files = sorted(
        [f for f in os.listdir(OUTPUT_BASE) if f.endswith("_matches.json")],
        reverse=True
    )
    if files:
        return os.path.join(OUTPUT_BASE, files[0])
    return None


def load_jobs_from_pipeline(json_path):
    """Load jobs from pipeline JSON and normalise fields."""
    with open(json_path) as f:
        raw = json.load(f)
    jobs = []
    for entry in raw:
        jobs.append({
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
    return jobs


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  AIRTABLE SYNC — {TODAY}")
    print(f"{'='*60}")

    key, base_id, table_id = load_auth()

    # --base flag: override/store base_id from command line
    if "--base" in sys.argv:
        idx     = sys.argv.index("--base")
        base_id = sys.argv[idx + 1].strip()
        save_base_id(base_id, "")  # persist; table_id will be discovered
        table_id = ""
        print(f"  Using base_id: {base_id}")

    # --setup flag: create/verify table schema only
    if "--setup" in sys.argv:
        base_id, table_id = ensure_base(key, base_id, table_id)
        print(f"\n  Setup complete.")
        print(f"  Base ID:  {base_id}")
        print(f"  Table ID: {table_id}")
        print(f"  View at:  https://airtable.com/{base_id}")
        return

    # Find input JSON
    if "--json" in sys.argv:
        idx      = sys.argv.index("--json")
        json_path = sys.argv[idx + 1]
    else:
        json_path = find_today_json()

    if not json_path or not os.path.exists(json_path):
        print(f"  ERROR: No match JSON found. Run job_pipeline.py first.")
        print(f"  Or specify: python airtable_sync.py --json /path/to/matches.json")
        sys.exit(1)

    print(f"  Input: {json_path}")

    # Ensure base/table exist
    base_id, table_id = ensure_base(key, base_id, table_id)

    # Load jobs
    jobs = load_jobs_from_pipeline(json_path)
    print(f"  Loaded {len(jobs)} jobs from pipeline")

    # Fetch existing URLs for dedup
    print("  Checking for duplicates...")
    existing_urls = fetch_existing_urls(key, base_id, table_id)
    print(f"  Found {len(existing_urls)} existing records in Airtable")

    # Create new records
    created_pairs = batch_create_records(key, base_id, jobs, existing_urls)

    # Upload PDFs
    pairs_with_pdfs = [(j, rid) for j, rid in created_pairs
                       if j.get("resume_pdf") or j.get("cover_pdf")]
    if pairs_with_pdfs:
        print(f"\n  Uploading PDFs for {len(pairs_with_pdfs)} records...")
        upload_pdfs(key, base_id, table_id, pairs_with_pdfs)

    print(f"\n{'='*60}")
    print(f"  Sync complete!")
    print(f"  New records created: {len(created_pairs)}")
    print(f"  Duplicates skipped:  {len(jobs) - len(created_pairs)}")
    print(f"  View at: https://airtable.com/{base_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
