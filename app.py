import os
import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import time

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "https://pipedrive-button.onrender.com")
PIPEDRIVE_CLIENT_ID = os.getenv("PIPEDRIVE_CLIENT_ID", "")
REDIRECT_URI = f"{BASE_URL}/oauth/callback"
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

FIELD_KEYS = {
    "deal": {"deal_context": "e637e09d69529de9a304c5a82a7a16eccee68c83"},          # put API key here
    "person": {"background": "ea9b03ac0608816c4dbe05a2ce5109ff8276aab8"},          # put API key here
    "organization": {"about": "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2"},         # put API key here
}

DB_PATH = "tokens.db"

def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            company_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

db_init()

def save_tokens(company_id: str, access_token: str, refresh_token: str, expires_in: int):
    expires_at = int(time.time()) + int(expires_in) - 60  # 60s safety margin
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO tokens(company_id, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?)",
        (company_id, access_token, refresh_token, expires_at),
    )
    conn.commit()
    conn.close()

def load_tokens(company_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM tokens WHERE company_id=?",
        (company_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}

def ai_write_summary(resource: str, record: dict) -> str:
    """
    Returns a short summary string for:
      - person -> 'Background'
      - organization -> 'About'
      - deal -> 'Deal Context'
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY env var")

    # Build the prompt from what we already have in Pipedrive
    # (Later we can enrich it with website text / web search)
    if resource == "person":
        name = record.get("name")
        emails = record.get("email")
        phones = record.get("phone")
        org = (record.get("org_id") or {}).get("name") if isinstance(record.get("org_id"), dict) else record.get("org_id")

        # Try to find a LinkedIn URL if it exists in the record (custom field or standard field)
        linkedin_url = None
        for k, v in record.items():
            if "linkedin" in str(k).lower() and isinstance(v, str) and v.startswith("http"):
                linkedin_url = v
                break

        user_text = f"""
Create a short professional background (3-6 sentences) for a CRM contact.

Person name: {name}
Org: {org}
Emails: {emails}
Phones: {phones}
LinkedIn URL (reference only; do not claim you read it unless content is provided): {linkedin_url}

Rules:
- If info is missing, be conservative and do not invent facts.
- Keep it concise and useful for sales.
- Output plain text only.
""".strip()

    elif resource == "organization":
        name = record.get("name")
        website = record.get("website") or record.get("websites")  # sometimes varies
        address = record.get("address")

        user_text = f"""
Write a short "About" paragraph (3-6 sentences) for a CRM organization.

Company name: {name}
Website: {website}
Address: {address}

Rules:
- If you cannot access the website content, do not invent specifics.
- Keep it concise, business-focused, no hype.
- Output plain text only.
""".strip()

    else:  # deal
        title = record.get("title")
        value = record.get("value")
        currency = record.get("currency")
        stage = record.get("stage_id")
        org = (record.get("org_id") or {}).get("name") if isinstance(record.get("org_id"), dict) else record.get("org_id")
        person = (record.get("person_id") or {}).get("name") if isinstance(record.get("person_id"), dict) else record.get("person_id")

        user_text = f"""
Write a short deal context note (3-6 sentences) for a CRM deal.

Deal title: {title}
Value: {value} {currency}
Stage: {stage}
Organization: {org}
Primary person: {person}

Rules:
- Do not invent facts beyond what is provided.
- Make it useful as context for the next call/email.
- Output plain text only.
""".strip()

    # Call OpenAI Responses API
    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You are an assistant that writes concise, factual CRM summaries."},
            {"role": "user", "content": user_text},
        ],
    )
    return resp.output_text.strip()

# ---------------------------
# Panel (embedded in Pipedrive)
# ---------------------------
@app.get("/panel")
def panel():
    return FileResponse("static/panel.html")


# ---------------------------
# Health (for Render checks)
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------
# OAuth (needed to get tokens)
# ---------------------------
@app.get("/oauth/start")
def oauth_start():
    if not PIPEDRIVE_CLIENT_ID:
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID env var"}, status_code=500)

    auth_url = (
        "https://oauth.pipedrive.com/oauth/authorize"
        f"?client_id={PIPEDRIVE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&state=demo"
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "No code in callback (user may have declined)"}, status_code=400)

    client_secret = os.getenv("PIPEDRIVE_CLIENT_SECRET", "")
    if not PIPEDRIVE_CLIENT_ID or not client_secret:
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID or PIPEDRIVE_CLIENT_SECRET"}, status_code=500)

    token_url = "https://oauth.pipedrive.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": PIPEDRIVE_CLIENT_ID,
        "client_secret": client_secret,
    }

    r = requests.post(token_url, data=payload, timeout=30)
    if r.status_code != 200:
        return JSONResponse(
            {"error": "Token exchange failed", "status": r.status_code, "body": r.text},
            status_code=400,
        )

    tokens = r.json()

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    expires_in = tokens.get("expires_in", 3600)

    # Get company_id from Pipedrive using the access token
    me = requests.get(
        "https://api.pipedrive.com/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    me.raise_for_status()
    company_id = str(me.json()["data"]["company_id"])

    save_tokens(company_id, access_token, refresh_token, int(expires_in))

    # Confirm success (do NOT display tokens)
    return {
        "ok": True,
        "got_access_token": bool(tokens.get("access_token")),
        "got_refresh_token": bool(tokens.get("refresh_token")),
        "expires_in": tokens.get("expires_in"),
        "note": "Next step: store tokens securely per company/user",
    }

@app.post("/api/populate")
async def api_populate(payload: dict):
    resource = payload.get("resource")           # "deal" / "person" / "organization"
    record_id = str(payload.get("id"))
    company_id = str(payload.get("companyId"))

    if resource not in ("deal", "person", "organization"):
        return JSONResponse({"error": "Unsupported resource"}, status_code=400)

    tokens = load_tokens(company_id)
    if not tokens:
        return JSONResponse({"error": "Not connected. Run /oauth/start once."}, status_code=401)

    access_token = tokens["access_token"]

    # Pipedrive endpoints
    base = "https://api.pipedrive.com/v1"
    if resource == "deal":
        get_url = f"{base}/deals/{record_id}"
        put_url = f"{base}/deals/{record_id}"
        field_key = FIELD_KEYS["deal"]["deal_context"]
    elif resource == "person":
        get_url = f"{base}/persons/{record_id}"
        put_url = f"{base}/persons/{record_id}"
        field_key = FIELD_KEYS["person"]["background"]
    else:
        get_url = f"{base}/organizations/{record_id}"
        put_url = f"{base}/organizations/{record_id}"
        field_key = FIELD_KEYS["organization"]["about"]

    if not field_key:
        return JSONResponse({"error": "Field API key not configured yet."}, status_code=500)

    # Fetch record
    r = requests.get(get_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch record", "body": r.text}, status_code=400)

    data = r.json().get("data", {})
    current_value = data.get(field_key)

    # Only fill if empty
    if current_value not in (None, "", []):
        return {"ok": True, "message": "Field already filled. Nothing to do."}

    try:
        ai_text = ai_write_summary(resource, data)
    except Exception as e:
        return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

    update = {field_key: ai_text}

    u = requests.put(put_url, json=update, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update record", "body": u.text}, status_code=400)

    return {"ok": True, "message": "Done. Filled 1 field."}

# Static files (panel.html and any JS/CSS if you add later)
app.mount("/static", StaticFiles(directory="static"), name="static")