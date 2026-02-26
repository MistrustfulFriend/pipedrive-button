import os
import re
import json
import secrets
import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import time
from openai import OpenAI

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "https://pipedrive-button.onrender.com")
PIPEDRIVE_CLIENT_ID = os.getenv("PIPEDRIVE_CLIENT_ID", "")
REDIRECT_URI = f"{BASE_URL}/oauth/callback"
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ── Field definitions ─────────────────────────────────────────────────────────
# web_searchable: whether a targeted web search makes sense for this field
ORG_FIELDS = {
    "address":        {"key": "address",                                       "type": "address", "label": "Address",                   "web_searchable": True},
    "industry":       {"key": "industry",                                      "type": "enum",    "label": "Industry",                   "web_searchable": False},
    "annual_revenue": {"key": "annual_revenue",                                "type": "number",  "label": "Annual Revenue",             "web_searchable": True},
    "employee_count": {"key": "employee_count",                                "type": "number",  "label": "Number of Employees",        "web_searchable": True},
    "phone":          {"key": "phone",                                         "type": "phone",   "label": "Phone Number",               "web_searchable": True},
    "email":          {"key": "email",                                         "type": "email",   "label": "Email",                      "web_searchable": True},
    "email2":         {"key": "901f73bf1243fa0baa769a41aef100674e792616",      "type": "text",    "label": "Second Email",               "web_searchable": False},
    "linkedin":       {"key": "linkedin",                                      "type": "text",    "label": "LinkedIn Profile",           "web_searchable": True},
    "about":          {"key": "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2",      "type": "text",    "label": "About",                      "web_searchable": False},
    "culture":        {"key": "f2de3e23b45d3ffa67abf8fdea7564c14f6ff9bb",      "type": "text",    "label": "Company Culture & Values",   "web_searchable": False},
}

DEAL_FIELDS = {
    "deal_context": {"key": "e637e09d69529de9a304c5a82a7a16eccee68c83", "type": "text", "label": "Deal Context"},
}

# Web search queries per field — {company_name} and {domain} are substituted at runtime
WEB_SEARCH_QUERIES = {
    "phone":          '"{company_name}" phone number contact',
    "email":          '"{company_name}" contact email {domain}',
    "address":        '"{company_name}" office address headquarters {domain}',
    "linkedin":       '"{company_name}" LinkedIn company page site:linkedin.com/company',
    "annual_revenue": '"{company_name}" annual revenue turnover {domain}',
    "employee_count": '"{company_name}" number of employees headcount {domain}',
}

DB_PATH = "tokens.db"


# ──────────────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────────────

def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            company_id    TEXT PRIMARY KEY,
            access_token  TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at    INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state      TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()


db_init()


def save_tokens(company_id, access_token, refresh_token, expires_in):
    expires_at = int(time.time()) + int(expires_in) - 60
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO tokens(company_id, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?)",
        (company_id, access_token, refresh_token, expires_at),
    )
    conn.commit()
    conn.close()


def load_tokens(company_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM tokens WHERE company_id=?",
        (company_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


def save_oauth_state(state):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO oauth_states(state, created_at) VALUES (?, ?)", (state, int(time.time())))
    conn.commit()
    conn.close()


def consume_oauth_state(state):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (int(time.time()) - 600,))
    row = conn.execute("SELECT state FROM oauth_states WHERE state=?", (state,)).fetchone()
    if row:
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    conn.commit()
    conn.close()
    return row is not None


# ──────────────────────────────────────────────────────────────────────────────
# Pipedrive helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_industry_options(access_token: str) -> list:
    r = requests.get(
        "https://api.pipedrive.com/v1/organizationFields",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code != 200:
        return []
    for field in r.json().get("data", []):
        if field.get("key") == "industry":
            return field.get("options") or []
    return []


def is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    if isinstance(value, list) and all(not (v.get("value") or "").strip() for v in value if isinstance(v, dict)):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Website scraper
# ──────────────────────────────────────────────────────────────────────────────

def fetch_website_text(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
    except Exception:
        return ""

    html = r.text
    for tag in ["<script", "<style"]:
        while True:
            i = html.lower().find(tag)
            if i == -1:
                break
            j = html.lower().find("</", i)
            if j == -1:
                break
            k = html.find(">", j)
            if k == -1:
                break
            html = html[:i] + html[k + 1:]

    text = []
    in_tag = False
    for ch in html:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            text.append(ch)

    return " ".join("".join(text).split())[:10000]


# ──────────────────────────────────────────────────────────────────────────────
# AI extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

def build_field_instructions(fields: list, industry_options: list) -> str:
    lines = []
    for f in fields:
        info  = ORG_FIELDS[f]
        ftype = info["type"]

        if f == "industry":
            opts = ", ".join(f'"{o["label"]}"' for o in industry_options)
            lines.append(f'- "industry": Choose EXACTLY one from [{opts}]. Return null if none fit.')
        elif f == "annual_revenue":
            lines.append('- "annual_revenue": Annual revenue as a plain number (no symbols/commas). E.g. 5000000. Null if not found.')
        elif f == "employee_count":
            lines.append('- "employee_count": Number of employees as a plain integer. If a range, use the midpoint. Null if not found.')
        elif f == "phone":
            lines.append('- "phone": Primary phone number as a plain string including country code if present. Null if not found.')
        elif f == "email":
            lines.append('- "email": Primary contact email address. Null if not found.')
        elif f == "email2":
            lines.append('- "email2": A secondary/alternative contact email different from the primary. Null if not found.')
        elif f == "linkedin":
            lines.append('- "linkedin": Company LinkedIn URL (linkedin.com/company/...). Null if not found.')
        elif f == "address":
            lines.append('- "address": Full office/headquarters address as a single string. Null if not found.')
        elif f == "about":
            lines.append('- "about": 4-6 sentence plain-text description: what they do, industry, size, location, specialities.')
        elif f == "culture":
            lines.append('- "culture": 2-4 sentences on company culture, values, or work environment. Null if nothing relevant found.')
    return "\n".join(lines)


def parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# PASS 1 — Extract from website text
# ──────────────────────────────────────────────────────────────────────────────

def ai_extract_from_website(
    name: str,
    website_url: str,
    website_text: str,
    fields: list,
    industry_options: list,
) -> dict:
    prompt = f"""
You are extracting structured data from a company website for a CRM system.

Company name: {name}
Website: {website_url}

== WEBSITE TEXT (your ONLY source) ==
{website_text}
== END ==

Extract the following fields. Return a single JSON object.
Return null for any field not explicitly found in the text above.
Do NOT invent or infer — only use information present in the source.

{build_field_instructions(fields, industry_options)}

Return ONLY valid JSON, no markdown, no explanation.
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You are a precise data extraction assistant. Return only valid JSON. Never invent data."},
            {"role": "user", "content": prompt},
        ],
    )
    return parse_json_response(resp.output_text)


# ──────────────────────────────────────────────────────────────────────────────
# PASS 2 — Web search for remaining missing fields
# ──────────────────────────────────────────────────────────────────────────────

def ai_extract_from_web(
    name: str,
    website_url: str,
    domain: str,
    fields: list,
    industry_options: list,
) -> dict:
    """
    Uses OpenAI's web_search_preview tool to search for each missing field
    with a targeted, company-specific query. All searches happen in a single
    AI call — the model searches, reads results, and extracts a JSON object.

    Crucially: the prompt instructs the model to verify that results are
    genuinely about THIS company (matched by name + domain) before extracting.
    """

    # Build the search strategy description per field
    search_hints = []
    for f in fields:
        query = WEB_SEARCH_QUERIES.get(f, "")
        if query:
            q = query.format(company_name=name, domain=domain)
            search_hints.append(f'- For "{f}": search for: {q}')

    if not search_hints:
        return {}

    search_block = "\n".join(search_hints)
    field_instructions = build_field_instructions(fields, industry_options)

    prompt = f"""
You are a CRM data researcher. You need to find specific information about a company
by searching the web. The company details are:

  Company name: {name}
  Website: {website_url}
  Domain: {domain}

IMPORTANT ACCURACY RULE:
Before extracting any value, verify that the search result is genuinely about
THIS company — it must match both the company name AND the domain ({domain}).
If a result is about a different company with a similar name, ignore it entirely.
If you are not confident a result refers to this exact company, return null for that field.

Search strategy (use one search per field):
{search_block}

After searching, extract the following fields into a single JSON object.
Return null for any field you could not find with high confidence.

{field_instructions}

Return ONLY valid JSON, no markdown, no explanation.
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        input=[
            {
                "role": "system",
                "content": (
                    "You are a precise CRM data researcher. You use web search to find factual "
                    "company information. You only extract data you are confident belongs to the "
                    "specific company identified by name AND domain. You return only valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return parse_json_response(resp.output_text)


# ──────────────────────────────────────────────────────────────────────────────
# Deal summary
# ──────────────────────────────────────────────────────────────────────────────

def fetch_deal_notes(deal_id: str, headers: dict, date_from: str = "", date_to: str = "") -> list:
    """Fetch up to 50 most recent notes for a deal, newest first. Optionally filter by date range."""
    r = requests.get(
        "https://api.pipedrive.com/v1/notes",
        params={"deal_id": deal_id, "limit": 50, "sort": "add_time DESC"},
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        return []
    notes = r.json().get("data") or []
    return _filter_by_date(notes, "add_time", date_from, date_to)


def _filter_by_date(items: list, date_key: str, date_from: str, date_to: str) -> list:
    """Filter a list of dicts by a date field. Dates are YYYY-MM-DD strings."""
    if not date_from and not date_to:
        return items
    result = []
    for item in items:
        raw = (item.get(date_key) or "")[:10]
        if not raw:
            result.append(item)
            continue
        if date_from and raw < date_from:
            continue
        if date_to and raw > date_to:
            continue
        result.append(item)
    return result


def fetch_deal_activities(deal_id: str, headers: dict, date_from: str = "", date_to: str = "") -> list:
    """Fetch up to 50 most recent activities for a deal using v2 API. Optionally filter by date range."""
    r = requests.get(
        "https://api.pipedrive.com/api/v2/activities",
        params={"deal_id": deal_id, "limit": 50, "sort_by": "add_time", "sort_direction": "desc"},
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        return []
    activities = r.json().get("data") or []
    return _filter_by_date(activities, "due_date", date_from, date_to)


def clean_html(text: str) -> str:
    """Strip HTML tags from note content."""
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def format_notes_block(notes: list) -> str:
    if not notes:
        return ""
    lines = ["== NOTES =="]
    for n in notes:
        date    = (n.get("add_time") or "")[:10]
        content = clean_html(n.get("content", "")).strip()
        if content:
            lines.append(f"[{date}] {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_activities_block(activities: list) -> str:
    if not activities:
        return ""
    lines = ["== ACTIVITIES =="]
    for a in activities:
        date    = (a.get("due_date") or a.get("add_time") or "")[:10]
        atype   = a.get("type", "activity")
        subject = a.get("subject") or ""
        done    = "\u2713" if a.get("done") else "\u25cb"
        note_txt = clean_html(a.get("note", "")).strip()
        entry   = f"[{date}] {done} {atype.upper()}: {subject}"
        if note_txt and note_txt != subject:
            entry += f" \u2014 {note_txt[:300]}"
        lines.append(entry)
    return "\n".join(lines) if len(lines) > 1 else ""


def ai_write_deal_summary(record: dict, notes: list, activities: list) -> str:
    title       = record.get("title", "")
    value       = record.get("value", "")
    currency    = record.get("currency", "")
    probability = record.get("probability", "")
    close_date  = record.get("close_time") or record.get("expected_close_date") or ""
    if close_date:
        close_date = str(close_date)[:10]
    status      = record.get("status", "")
    pipeline    = (record.get("pipeline_id") or {}).get("name", "") if isinstance(record.get("pipeline_id"), dict) else ""
    stage       = (record.get("stage_id") or {}).get("name", "") if isinstance(record.get("stage_id"), dict) else str(record.get("stage_id") or "")
    owner       = (record.get("owner_id") or {}).get("name", "") if isinstance(record.get("owner_id"), dict) else ""
    org         = (record.get("org_id") or {}).get("name", "") if isinstance(record.get("org_id"), dict) else record.get("org_id") or ""
    person      = (record.get("person_id") or {}).get("name", "") if isinstance(record.get("person_id"), dict) else record.get("person_id") or ""

    # Build deal details block—only include lines where a value exists
    detail_lines = [f"Title:        {title}"]
    if value:
        val_str = f"{value:,}" if isinstance(value, (int, float)) else str(value)
        detail_lines.append(f"Value:        {val_str} {currency}".strip())
    if probability not in (None, "", "null"):
        detail_lines.append(f"Probability:  {probability}%")
    if close_date:
        detail_lines.append(f"Close date:   {close_date}")
    if status:
        detail_lines.append(f"Status:       {status}")
    if pipeline:
        detail_lines.append(f"Pipeline:     {pipeline}")
    if stage:
        detail_lines.append(f"Stage:        {stage}")
    if owner:
        detail_lines.append(f"Owner:        {owner}")
    if org:
        detail_lines.append(f"Organisation: {org}")
    if person:
        detail_lines.append(f"Contact:      {person}")

    deal_details = "\n".join(detail_lines)

    notes_block      = format_notes_block(notes)
    activities_block = format_activities_block(activities)
    has_history      = bool(notes_block or activities_block)

    history_section = ""
    if has_history:
        parts = [p for p in [notes_block, activities_block] if p]
        history_section = "\n\n" + "\n\n".join(parts)

    # Build mandatory-facts rule so the model is explicitly told what to include
    mandatory_parts = []
    if value:
        mandatory_parts.append("deal value")
    if probability not in (None, "", "null"):
        mandatory_parts.append("win probability")
    if close_date:
        mandatory_parts.append("expected close date")
    if stage:
        mandatory_parts.append("current stage")
    mandatory_str = ", ".join(mandatory_parts)
    mandatory_rule = (
        f"- Your summary MUST mention the following facts (if present): {mandatory_str}. "
        "Never omit these \xe2\x80\x94 they are the most important numbers for the sales team."
    ) if mandatory_parts else ""

    if has_history:
        instruction = (
            "Write a 4-7 sentence deal context summary for a sales team. "
            "Start with a one-sentence snapshot that includes the deal value, win probability, "
            "current stage, and expected close date. "
            "Then cover: what has happened so far, key discussion points or concerns raised, "
            "current status, and the logical next step. "
            "Be specific \xe2\x80\x94 reference actual dates, topics, and outcomes from the history. "
            "Plain text only, no bullet points."
        )
    else:
        instruction = (
            "Write a 3-5 sentence deal context note useful before a first sales call. "
            "Start with a one-sentence snapshot that includes the deal value, win probability, "
            "current stage, and expected close date. "
            "Plain text only, no bullet points."
        )

    prompt = f"""
{instruction}

== DEAL DETAILS ==
{deal_details}{history_section}

STRICT RULES:
- Use only information present above. Do not invent facts.
- Do not repeat field labels verbatim (e.g. don't write "Value: 50,000 EUR" \xe2\x80\x94 write "a \xe2\x82\xac50,000 deal").
- Do not say "according to the notes" or reference the data structure.
- Write as a natural, useful briefing paragraph.
{mandatory_rule}
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "system",
                "content": (
                    "You are a precise CRM assistant writing deal briefings for sales reps. "
                    "You synthesize deal data, notes and activity history into a clear, actionable summary. "
                    "You ALWAYS include the deal value, win probability, stage, and close date when they are available. "
                    "You never invent facts."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return resp.output_text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Value formatter
# ──────────────────────────────────────────────────────────────────────────────

def format_value_for_pipedrive(field_name: str, field_type: str, raw_value, industry_options: list):
    if raw_value is None:
        return None

    if field_type == "enum":
        label = str(raw_value).strip()
        for opt in industry_options:
            if opt.get("label", "").lower() == label.lower():
                return opt["id"]
        return None

    elif field_type == "phone":
        return [{"value": str(raw_value).strip(), "primary": True, "label": "work"}]

    elif field_type == "email":
        return [{"value": str(raw_value).strip(), "primary": True, "label": "work"}]

    elif field_type == "number":
        try:
            cleaned = re.sub(r"[^\d.]", "", str(raw_value))
            return float(cleaned) if "." in cleaned else int(cleaned)
        except (ValueError, TypeError):
            return None

    else:
        return str(raw_value).strip() if str(raw_value).strip() else None


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/panel")
def panel():
    return FileResponse("static/panel.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/oauth/start")
def oauth_start():
    if not PIPEDRIVE_CLIENT_ID:
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID env var"}, status_code=500)
    state = secrets.token_urlsafe(32)
    save_oauth_state(state)
    auth_url = (
        "https://oauth.pipedrive.com/oauth/authorize"
        f"?client_id={PIPEDRIVE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&state={state}"
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state", "")

    if not state or not consume_oauth_state(state):
        return JSONResponse({"error": "Invalid or expired state — please start again via /oauth/start."}, status_code=400)
    if not code:
        return JSONResponse({"error": "No authorisation code returned. User may have declined."}, status_code=400)

    client_secret = os.getenv("PIPEDRIVE_CLIENT_SECRET", "")
    if not PIPEDRIVE_CLIENT_ID or not client_secret:
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID or PIPEDRIVE_CLIENT_SECRET env var"}, status_code=500)

    r = requests.post(
        "https://oauth.pipedrive.com/oauth/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "client_id":     PIPEDRIVE_CLIENT_ID,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if r.status_code != 200:
        return JSONResponse({"error": "Token exchange failed", "status": r.status_code, "body": r.text}, status_code=400)

    tokens        = r.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    expires_in    = tokens.get("expires_in", 3600)

    me = requests.get(
        "https://api.pipedrive.com/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    me.raise_for_status()
    company_id = str(me.json()["data"]["company_id"])

    save_tokens(company_id, access_token, refresh_token, int(expires_in))
    return {"ok": True, "company_id": company_id, "note": "OAuth complete. Tokens saved."}


@app.post("/api/populate")
async def api_populate(payload: dict):
    resource   = payload.get("resource")
    record_id  = str(payload.get("id"))
    company_id = str(payload.get("companyId"))

    # Normalise both spellings Pipedrive may send
    if resource in ("organisation", "organization"):
        resource = "organization"

    if resource not in ("deal", "person", "organization"):
        return JSONResponse({"error": "Unsupported resource"}, status_code=400)

    if resource == "person":
        return JSONResponse(
            {"error": "Person enrichment is not available. Only Organisation and Deal fields can be populated."},
            status_code=400,
        )

    tokens = load_tokens(company_id)
    if not tokens:
        return JSONResponse({"error": "Not connected. Run /oauth/start once."}, status_code=401)

    access_token = tokens["access_token"]
    headers      = {"Authorization": f"Bearer {access_token}"}
    base         = "https://api.pipedrive.com/v1"

    # ── Deal ─────────────────────────────────────────────────────────────────
    if resource == "deal":
        r = requests.get(f"{base}/deals/{record_id}", headers=headers, timeout=30)
        if r.status_code != 200:
            return JSONResponse({"error": "Failed to fetch deal", "body": r.text}, status_code=400)
        data       = r.json().get("data", {})
        target_key = DEAL_FIELDS["deal_context"]["key"]

        if not is_empty(data.get(target_key)):
            return {"ok": True, "message": "Deal context already filled. Nothing to do."}

        # Fetch notes and activities to enrich the summary (optionally date-filtered)
        date_from  = str(payload.get("date_from") or "")[:10]
        date_to    = str(payload.get("date_to")   or "")[:10]
        notes      = fetch_deal_notes(record_id, headers, date_from, date_to)
        activities = fetch_deal_activities(record_id, headers, date_from, date_to)

        try:
            ai_text = ai_write_deal_summary(data, notes, activities)
        except Exception as e:
            return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

        u = requests.put(f"{base}/deals/{record_id}", json={target_key: ai_text}, headers=headers, timeout=30)
        if u.status_code != 200:
            return JSONResponse({"error": "Failed to update deal", "body": u.text}, status_code=400)

        has_history = bool(notes or activities)
        source_note = " (based on notes & activity history)" if has_history else " (no history found — used deal details only)"
        return {
            "ok":           True,
            "message":      f"Done. Deal context populated{source_note}.",
            "filled_website": ["Deal Context"],
            "filled_web":     [],
            "not_found":      [],
        }

    # ── Organisation ─────────────────────────────────────────────────────────
    r = requests.get(f"{base}/organizations/{record_id}", headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch organisation", "body": r.text}, status_code=400)

    data = r.json().get("data", {})

    # Which fields need filling?
    fields_to_fill = [
        name for name, info in ORG_FIELDS.items()
        if is_empty(data.get(info["key"]))
    ]

    if not fields_to_fill:
        return {"ok": True, "message": "All fields already filled. Nothing to do."}

    # Get website URL and domain
    website_url = data.get("website") or ""
    if isinstance(website_url, list):
        website_url = website_url[0].get("value", "") if website_url else ""

    if not website_url:
        return JSONResponse(
            {"error": "No website found on this organisation record. Please add one first."},
            status_code=400,
        )

    # Extract domain for anchoring web searches (e.g. "eaces.de")
    domain_match = re.search(r"https?://(?:www\.)?([^/]+)", website_url)
    domain = domain_match.group(1) if domain_match else website_url

    # Fetch industry options if needed
    industry_options = []
    if "industry" in fields_to_fill:
        industry_options = get_industry_options(access_token)

    org_name = data.get("name", "")

    # ── PASS 1: extract from website ─────────────────────────────────────────
    website_text = fetch_website_text(website_url)
    if not website_text or len(website_text) < 100:
        return JSONResponse(
            {"error": f"Could not read content from {website_url}. The site may block requests or require JavaScript."},
            status_code=400,
        )

    try:
        extracted = ai_extract_from_website(
            name=org_name,
            website_url=website_url,
            website_text=website_text,
            fields=fields_to_fill,
            industry_options=industry_options,
        )
    except Exception as e:
        return JSONResponse({"error": "AI extraction (website) failed", "details": str(e)}, status_code=500)

    # Which fields are still missing after pass 1?
    still_missing = [
        f for f in fields_to_fill
        if extracted.get(f) is None and ORG_FIELDS[f].get("web_searchable")
    ]

    # ── PASS 2: web search for remaining fields ───────────────────────────────
    web_extracted = {}
    if still_missing:
        try:
            web_extracted = ai_extract_from_web(
                name=org_name,
                website_url=website_url,
                domain=domain,
                fields=still_missing,
                industry_options=industry_options,
            )
        except Exception:
            pass  # web search is best-effort — don't fail the whole request

    # Merge: website data takes priority, web search fills the gaps
    for f in still_missing:
        if web_extracted.get(f) is not None and extracted.get(f) is None:
            extracted[f] = web_extracted[f]

    # ── Build Pipedrive update payload ────────────────────────────────────────
    update_payload = {}
    filled_website = []
    filled_web     = []
    not_found      = []

    for field_name in fields_to_fill:
        raw_value = extracted.get(field_name)
        if raw_value is None:
            not_found.append(ORG_FIELDS[field_name]["label"])
            continue

        formatted = format_value_for_pipedrive(
            field_name,
            ORG_FIELDS[field_name]["type"],
            raw_value,
            industry_options,
        )
        if formatted is None:
            not_found.append(ORG_FIELDS[field_name]["label"])
            continue

        update_payload[ORG_FIELDS[field_name]["key"]] = formatted
        label = ORG_FIELDS[field_name]["label"]
        if field_name in still_missing and web_extracted.get(field_name) is not None:
            filled_web.append(label)
        else:
            filled_website.append(label)

    if not update_payload:
        return {"ok": True, "message": f"No data found for: {', '.join(not_found)}."}

    # Write to Pipedrive
    u = requests.put(
        f"{base}/organizations/{record_id}",
        json=update_payload,
        headers=headers,
        timeout=30,
    )
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update organisation", "body": u.text}, status_code=400)

    # Build a clear, informative message
    parts = []
    if filled_website:
        parts.append(f"From website: {', '.join(filled_website)}")
    if filled_web:
        parts.append(f"From web search: {', '.join(filled_web)}")
    if not_found:
        parts.append(f"Not found: {', '.join(not_found)}")

    total = len(filled_website) + len(filled_web)
    return {
        "ok": True,
        "message": f"{total} field{'s' if total != 1 else ''} populated. " + ". ".join(parts) + ".",
        "filled_website": filled_website,
        "filled_web":     filled_web,
        "not_found":      not_found,
    }


@app.post("/api/chat")
async def api_chat(payload: dict):
    """
    Lightweight chat proxy. Receives:
      - messages: list of {role, content} (full history)
      - context:  optional deal/org context string prepended as system context
      - companyId: for auth check
    Returns: {reply: str}
    """
    company_id = str(payload.get("companyId", ""))
    messages   = payload.get("messages", [])
    context    = payload.get("context", "").strip()

    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)

    # Optional: verify company has a valid token (soft check, don't block chat)
    tokens = load_tokens(company_id) if company_id else None

    system_content = (
        "You are a helpful sales assistant embedded inside Pipedrive CRM. "
        "You help sales reps understand their deals, draft emails, prepare for calls, "
        "and answer questions about contacts and organisations. "
        "Be concise and practical. Use plain text unless the user asks for formatting."
    )
    if context:
        system_content += f"\n\n== CURRENT RECORD CONTEXT ==\n{context}"

    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": system_content},
                *messages,
            ],
        )
        return {"reply": resp.output_text.strip()}
    except Exception as e:
        return JSONResponse({"error": "AI request failed", "details": str(e)}, status_code=500)


@app.post("/api/context")
async def api_context(payload: dict):
    """
    Returns a plain-text context string for the current record.
    Used by the chat to ground conversations in real CRM data.
    """
    resource   = payload.get("resource", "")
    record_id  = str(payload.get("id", ""))
    company_id = str(payload.get("companyId", ""))

    if resource in ("organisation", "organization"):
        resource = "organization"

    tokens = load_tokens(company_id)
    if not tokens:
        return {"context": ""}

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    base    = "https://api.pipedrive.com/v1"
    lines   = []

    try:
        if resource == "deal":
            r = requests.get(f"{base}/deals/{record_id}", headers=headers, timeout=15)
            if r.status_code == 200:
                d = r.json().get("data", {})
                lines.append(f"Record type: Deal")
                if d.get("title"):       lines.append(f"Title: {d['title']}")
                if d.get("value"):       lines.append(f"Value: {d['value']} {d.get('currency','')}")
                if d.get("probability"): lines.append(f"Win probability: {d['probability']}%")
                close = d.get("close_time") or d.get("expected_close_date")
                if close: lines.append(f"Expected close: {str(close)[:10]}")
                if d.get("status"):      lines.append(f"Status: {d['status']}")
                stage = d.get("stage_id")
                if isinstance(stage, dict): lines.append(f"Stage: {stage.get('name','')}")
                org = d.get("org_id")
                if isinstance(org, dict): lines.append(f"Organisation: {org.get('name','')}")
                person = d.get("person_id")
                if isinstance(person, dict): lines.append(f"Contact: {person.get('name','')}")
                owner = d.get("owner_id")
                if isinstance(owner, dict): lines.append(f"Owner: {owner.get('name','')}")
                # Custom deal context field
                ctx_key = DEAL_FIELDS["deal_context"]["key"]
                if d.get(ctx_key): lines.append(f"\nDeal context:\n{d[ctx_key]}")
                # Recent notes
                notes = fetch_deal_notes(record_id, headers)
                nb = format_notes_block(notes)
                if nb: lines.append("\n" + nb)
                # Recent activities
                acts = fetch_deal_activities(record_id, headers)
                ab = format_activities_block(acts)
                if ab: lines.append("\n" + ab)

        elif resource == "organization":
            r = requests.get(f"{base}/organizations/{record_id}", headers=headers, timeout=15)
            if r.status_code == 200:
                d = r.json().get("data", {})
                lines.append(f"Record type: Organisation")
                if d.get("name"):    lines.append(f"Name: {d['name']}")
                if d.get("address"): lines.append(f"Address: {d['address']}")
                website = d.get("website") or ""
                if isinstance(website, list): website = website[0].get("value","") if website else ""
                if website: lines.append(f"Website: {website}")
                # Custom fields
                for fname, finfo in ORG_FIELDS.items():
                    val = d.get(finfo["key"])
                    if val and fname not in ("address",):
                        if isinstance(val, list):
                            val = ", ".join(v.get("value","") for v in val if isinstance(v,dict))
                        if val: lines.append(f"{finfo['label']}: {val}")
    except Exception:
        pass

    return {"context": "\n".join(lines)}


@app.post("/api/chat")
async def api_chat(payload: dict):
    """
    Chat proxy. Receives:
      - messages:  [{role, content}]  full conversation history
      - context:   optional CRM context string prepended as system context
      - companyId: for logging/auth
    Returns: {reply: str}
    """
    messages   = payload.get("messages", [])
    context    = (payload.get("context") or "").strip()
    company_id = str(payload.get("companyId", ""))

    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)

    system = (
        "You are a helpful sales assistant embedded inside Pipedrive CRM. "
        "You help sales reps understand their deals and organisations, draft emails, "
        "prepare for calls, and answer questions. "
        "Be concise and practical. Use plain text unless the user asks for formatting. "
        "When drafting emails, write the full email including a subject line."
    )
    if context:
        system += f"\n\n== CURRENT RECORD ==\n{context}"

    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": system},
                *messages,
            ],
        )
        return {"reply": resp.output_text.strip()}
    except Exception as e:
        return JSONResponse({"error": "AI request failed", "details": str(e)}, status_code=500)


app.mount("/static", StaticFiles(directory="static"), name="static")