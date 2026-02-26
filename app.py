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

# ── Field keys ────────────────────────────────────────────────────────────────
# Standard Pipedrive fields use their plain name as the key.
# Custom fields use their 40-char hash.
ORG_FIELDS = {
    # Standard fields
    "address":        {"key": "address",        "type": "address",  "label": "Address"},
    "industry":       {"key": "industry",        "type": "enum",     "label": "Industry"},
    "annual_revenue": {"key": "annual_revenue",  "type": "number",   "label": "Annual Revenue"},
    "employee_count": {"key": "employee_count",  "type": "number",   "label": "Number of Employees"},
    # Standard fields that need special array format
    "phone":          {"key": "phone",           "type": "phone",    "label": "Phone Number"},
    "email":          {"key": "email",           "type": "email",    "label": "Email"},
    # Custom fields (hashes)
    "about":          {"key": "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2", "type": "text",  "label": "About"},
    "email2":         {"key": "901f73bf1243fa0baa769a41aef100674e792616",  "type": "text",  "label": "Second Email"},
    "linkedin":       {"key": "linkedin",                                   "type": "text",  "label": "LinkedIn Profile"},
    "culture":        {"key": "f2de3e23b45d3ffa67abf8fdea7564c14f6ff9bb",  "type": "text",  "label": "Company Culture & Values"},
}

DEAL_FIELDS = {
    "deal_context": {"key": "e637e09d69529de9a304c5a82a7a16eccee68c83", "type": "text", "label": "Deal Context"},
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

def get_industry_options(access_token: str) -> list[dict]:
    """
    Fetches the list of valid options for the Industry enum field.
    Returns a list of {"id": int, "label": str} dicts.
    """
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
    """Returns True if a Pipedrive field value counts as empty."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    # Phone/email arrays: treat as empty if all entries have no value
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
# AI extraction
# ──────────────────────────────────────────────────────────────────────────────

def ai_extract_org_fields(
    name: str,
    website_url: str,
    website_text: str,
    fields_to_fill: list[str],
    industry_options: list[dict],
) -> dict:
    """
    Asks the AI to extract all requested fields from the website text in one call.
    Returns a dict of {field_name: extracted_value}.
    For industry, returns the label string (we map to ID afterwards).
    """

    # Build the list of fields to extract, with instructions per type
    field_instructions = []
    for f in fields_to_fill:
        info = ORG_FIELDS[f]
        label = info["label"]
        ftype = info["type"]

        if ftype == "enum" and f == "industry":
            options_str = ", ".join(f'"{o["label"]}"' for o in industry_options)
            field_instructions.append(
                f'- "{f}": The industry. Choose EXACTLY one label from this list: [{options_str}]. '
                f'If none fit, return null.'
            )
        elif ftype == "number" and f == "annual_revenue":
            field_instructions.append(
                f'- "{f}": Annual revenue as a plain number (no currency symbol, no commas). '
                f'E.g. 5000000. Return null if not found.'
            )
        elif ftype == "number" and f == "employee_count":
            field_instructions.append(
                f'- "{f}": Number of employees as a plain integer. '
                f'If a range is given (e.g. "50-200"), use the midpoint. Return null if not found.'
            )
        elif ftype == "phone":
            field_instructions.append(
                f'- "{f}": Primary phone number as a plain string including country code if present. '
                f'Return null if not found.'
            )
        elif ftype in ("email", "text") and f == "email":
            field_instructions.append(
                f'- "{f}": Primary contact email address. Return null if not found.'
            )
        elif f == "email2":
            field_instructions.append(
                f'- "{f}": A secondary or alternative contact email (different from the primary). '
                f'Return null if not found.'
            )
        elif f == "linkedin":
            field_instructions.append(
                f'- "{f}": The company LinkedIn profile URL (linkedin.com/company/...). '
                f'Return null if not found.'
            )
        elif f == "address":
            field_instructions.append(
                f'- "{f}": Full office/headquarters address as a single string. Return null if not found.'
            )
        elif f == "about":
            field_instructions.append(
                f'- "{f}": A 4-6 sentence plain-text company description covering what they do, '
                f'industry, size, location, and specialities. Only from the source text.'
            )
        elif f == "culture":
            field_instructions.append(
                f'- "{f}": 2-4 sentences describing the company culture, values, or work environment '
                f'based only on what is mentioned in the source text. Return null if nothing relevant found.'
            )

    fields_block = "\n".join(field_instructions)

    prompt = f"""
You are extracting structured data from a company website for a CRM system.

Company name: {name}
Website: {website_url}

== WEBSITE TEXT (your only source) ==
{website_text}
== END OF SOURCE ==

Extract the following fields and return a single JSON object.
Use null for any field you cannot find in the source text.
Do NOT invent or guess — only use information explicitly present above.

Fields to extract:
{fields_block}

Return ONLY a valid JSON object, no explanation, no markdown, no code fences.
Example format: {{"about": "...", "industry": "Technology", "employee_count": 150, "phone": null}}
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "system",
                "content": (
                    "You are a precise data extraction assistant. You return only valid JSON. "
                    "You never invent data — if it is not in the source text, you return null for that field."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    raw = resp.output_text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def ai_write_deal_summary(record: dict) -> str:
    title    = record.get("title", "")
    value    = record.get("value", "")
    currency = record.get("currency", "")
    stage    = record.get("stage_id", "")
    org      = (record.get("org_id") or {}).get("name", "") if isinstance(record.get("org_id"), dict) else record.get("org_id") or ""
    person   = (record.get("person_id") or {}).get("name", "") if isinstance(record.get("person_id"), dict) else record.get("person_id") or ""

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You write short factual CRM deal context notes. Never invent anything."},
            {"role": "user", "content": (
                f"Write a 3-5 sentence deal context note useful before a sales call. Plain text only.\n\n"
                f"Deal: {title}\nValue: {value} {currency}\nStage: {stage}\nOrg: {org}\nContact: {person}"
            )},
        ],
    )
    return resp.output_text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Value formatters — converts AI output to the format Pipedrive expects
# ──────────────────────────────────────────────────────────────────────────────

def format_value_for_pipedrive(field_name: str, field_type: str, raw_value, industry_options: list[dict]):
    """
    Converts the AI-extracted value to the correct Pipedrive API format.
    Returns None if the value should be skipped.
    """
    if raw_value is None:
        return None

    if field_type == "enum":
        # Must send the option ID, not the label
        label = str(raw_value).strip()
        for opt in industry_options:
            if opt.get("label", "").lower() == label.lower():
                return opt["id"]
        return None  # label not found in options — skip

    elif field_type == "phone":
        # Pipedrive expects an array: [{"value": "...", "primary": true}]
        return [{"value": str(raw_value).strip(), "primary": True, "label": "work"}]

    elif field_type == "email":
        return [{"value": str(raw_value).strip(), "primary": True, "label": "work"}]

    elif field_type == "number":
        try:
            # Strip any non-numeric characters just in case
            cleaned = re.sub(r"[^\d.]", "", str(raw_value))
            return float(cleaned) if "." in cleaned else int(cleaned)
        except (ValueError, TypeError):
            return None

    else:
        # text, address, and everything else
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
        data         = r.json().get("data", {})
        target_key   = DEAL_FIELDS["deal_context"]["key"]

        if not is_empty(data.get(target_key)):
            return {"ok": True, "message": "Deal context already filled. Nothing to do."}

        try:
            ai_text = ai_write_deal_summary(data)
        except Exception as e:
            return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

        u = requests.put(f"{base}/deals/{record_id}", json={target_key: ai_text}, headers=headers, timeout=30)
        if u.status_code != 200:
            return JSONResponse({"error": "Failed to update deal", "body": u.text}, status_code=400)

        return {"ok": True, "message": "Done. Deal context populated."}

    # ── Organisation ─────────────────────────────────────────────────────────
    r = requests.get(f"{base}/organizations/{record_id}", headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch organisation", "body": r.text}, status_code=400)

    data = r.json().get("data", {})

    # Resolve which fields actually need filling (skip already-filled ones)
    fields_to_fill = []
    for field_name, field_info in ORG_FIELDS.items():
        current = data.get(field_info["key"])
        if is_empty(current):
            fields_to_fill.append(field_name)

    if not fields_to_fill:
        return {"ok": True, "message": "All fields already filled. Nothing to do."}

    # Get the website URL
    website_url = data.get("website") or ""
    if isinstance(website_url, list):
        website_url = website_url[0].get("value", "") if website_url else ""

    if not website_url:
        return JSONResponse(
            {"error": "No website found on this organisation record. Please add one first."},
            status_code=400,
        )

    # Scrape the website
    website_text = fetch_website_text(website_url)
    if not website_text or len(website_text) < 100:
        return JSONResponse(
            {"error": f"Could not read content from {website_url}. The site may be blocking requests or require JavaScript."},
            status_code=400,
        )

    # Fetch industry options if industry needs filling
    industry_options = []
    if "industry" in fields_to_fill:
        industry_options = get_industry_options(access_token)

    # Ask AI to extract all fields in one call
    try:
        extracted = ai_extract_org_fields(
            name=data.get("name", ""),
            website_url=website_url,
            website_text=website_text,
            fields_to_fill=fields_to_fill,
            industry_options=industry_options,
        )
    except Exception as e:
        return JSONResponse({"error": "AI extraction failed", "details": str(e)}, status_code=500)

    # Build the update payload, converting each value to Pipedrive format
    update_payload = {}
    filled = []
    skipped = []

    for field_name in fields_to_fill:
        raw_value = extracted.get(field_name)
        if raw_value is None:
            skipped.append(ORG_FIELDS[field_name]["label"])
            continue

        formatted = format_value_for_pipedrive(
            field_name,
            ORG_FIELDS[field_name]["type"],
            raw_value,
            industry_options,
        )
        if formatted is None:
            skipped.append(ORG_FIELDS[field_name]["label"])
            continue

        update_payload[ORG_FIELDS[field_name]["key"]] = formatted
        filled.append(ORG_FIELDS[field_name]["label"])

    if not update_payload:
        return {
            "ok": True,
            "message": f"No data found on the website for: {', '.join(skipped)}.",
        }

    # Write back to Pipedrive
    u = requests.put(
        f"{base}/organizations/{record_id}",
        json=update_payload,
        headers=headers,
        timeout=30,
    )
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update organisation", "body": u.text}, status_code=400)

    msg = f"Filled: {', '.join(filled)}."
    if skipped:
        msg += f" Not found on website: {', '.join(skipped)}."

    return {"ok": True, "message": msg}


app.mount("/static", StaticFiles(directory="static"), name="static")