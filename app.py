import os
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

# Custom field API keys
FIELD_KEYS = {
    "deal": {
        "deal_context": "e637e09d69529de9a304c5a82a7a16eccee68c83",
    },
    "person": {
        "linkedin": "6c01a20f553c1d1ab860a7396a65f55667d623d8",   # LinkedIn URL field
        "background": "ea9b03ac0608816c4dbe05a2ce5109ff8276aab8",  # Background field (target)
    },
    "organization": {
        "linkedin": "dce5d063616e3008d850b211ef4072181a02e02e",    # LinkedIn URL field
        "website": "website",                                        # Website field (standard)
        "about": "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2",        # About field (target)
    },
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


def fetch_website_text(url: str) -> str:
    """Fetch and return cleaned plain text from a URL (max 8000 chars)."""
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

    # Remove script and style blocks
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

    # Strip remaining HTML tags
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

    cleaned = " ".join("".join(text).split())
    return cleaned[:8000]


def ai_write_summary(resource: str, record: dict, extra_context: dict = None) -> str:
    """
    Generate a short AI summary for a CRM record.
    extra_context can contain scraped text from LinkedIn or website.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY env var")

    extra_context = extra_context or {}

    if resource == "person":
        name = record.get("name")
        emails = record.get("email")
        phones = record.get("phone")
        org = (
            (record.get("org_id") or {}).get("name")
            if isinstance(record.get("org_id"), dict)
            else record.get("org_id")
        )
        linkedin_url = record.get(FIELD_KEYS["person"]["linkedin"], "")
        linkedin_text = extra_context.get("linkedin_text", "")

        user_text = f"""
Create a short professional background (3-6 sentences) for a CRM contact.

Person name: {name}
Organisation: {org}
Emails: {emails}
Phones: {phones}
LinkedIn URL: {linkedin_url}
{f"LinkedIn page content (use this as primary source):{chr(10)}{linkedin_text}" if linkedin_text else ""}

Rules:
- Use only facts that are present in the data above; do not invent anything.
- Keep it concise and useful for a sales context.
- Output plain text only.
""".strip()

    elif resource == "organization":
        name = record.get("name")
        website_url = record.get("website") or record.get(FIELD_KEYS["organization"]["website"]) or ""
        address = record.get("address")
        linkedin_url = record.get(FIELD_KEYS["organization"]["linkedin"], "")
        website_text = extra_context.get("website_text", "")
        linkedin_text = extra_context.get("linkedin_text", "")

        context_block = ""
        if website_text:
            context_block += f"\nWebsite content (use as primary source):\n{website_text}\n"
        if linkedin_text:
            context_block += f"\nLinkedIn page content:\n{linkedin_text}\n"

        user_text = f"""
Write a short "About" paragraph (3-6 sentences) for a CRM organisation.

Company name: {name}
Website: {website_url}
LinkedIn URL: {linkedin_url}
Address: {address}
{context_block}

Rules:
- Use only facts present in the data above; do not invent specifics.
- Keep it concise and business-focused.
- Output plain text only.
""".strip()

    else:  # deal
        title = record.get("title")
        value = record.get("value")
        currency = record.get("currency")
        stage = record.get("stage_id")
        org = (
            (record.get("org_id") or {}).get("name")
            if isinstance(record.get("org_id"), dict)
            else record.get("org_id")
        )
        person = (
            (record.get("person_id") or {}).get("name")
            if isinstance(record.get("person_id"), dict)
            else record.get("person_id")
        )

        user_text = f"""
Write a short deal context note (3-6 sentences) for a CRM deal.

Deal title: {title}
Value: {value} {currency}
Stage: {stage}
Organisation: {org}
Primary person: {person}

Rules:
- Do not invent facts beyond what is provided.
- Make it useful as context for the next call/email.
- Output plain text only.
""".strip()

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
# OAuth
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

    me = requests.get(
        "https://api.pipedrive.com/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    me.raise_for_status()
    company_id = str(me.json()["data"]["company_id"])

    save_tokens(company_id, access_token, refresh_token, int(expires_in))

    return {
        "ok": True,
        "got_access_token": bool(tokens.get("access_token")),
        "got_refresh_token": bool(tokens.get("refresh_token")),
        "expires_in": tokens.get("expires_in"),
        "note": "OAuth complete. Tokens saved.",
    }


# ---------------------------
# Main populate endpoint
# ---------------------------
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
    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://api.pipedrive.com/v1"

    # ── Determine target field key ──────────────────────────────────────────
    if resource == "deal":
        get_url = f"{base}/deals/{record_id}"
        put_url = f"{base}/deals/{record_id}"
        target_field = FIELD_KEYS["deal"]["deal_context"]

    elif resource == "person":
        get_url = f"{base}/persons/{record_id}"
        put_url = f"{base}/persons/{record_id}"
        target_field = FIELD_KEYS["person"]["background"]

    else:  # organization
        get_url = f"{base}/organizations/{record_id}"
        put_url = f"{base}/organizations/{record_id}"
        target_field = FIELD_KEYS["organization"]["about"]

    if not target_field:
        return JSONResponse({"error": "Target field API key not configured."}, status_code=500)

    # ── Fetch the record ────────────────────────────────────────────────────
    r = requests.get(get_url, headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch record", "body": r.text}, status_code=400)

    data = r.json().get("data", {})

    # ── Guard: skip if target field is already filled ───────────────────────
    current_value = data.get(target_field)
    if current_value not in (None, "", []):
        return {"ok": True, "message": "Field already filled. Nothing to do."}

    # ── Gather extra context (LinkedIn / website scraping) ──────────────────
    extra_context: dict = {}

    if resource == "person":
        linkedin_url = data.get(FIELD_KEYS["person"]["linkedin"], "")
        if linkedin_url:
            # LinkedIn blocks scrapers; we pass the URL to the AI for context
            # but don't attempt to scrape it (returns 999 / redirect wall).
            # If you add a LinkedIn scraping service later, call it here.
            extra_context["linkedin_text"] = ""  # placeholder for future enrichment

    elif resource == "organization":
        # 1. Try website
        website_url = data.get("website") or data.get(FIELD_KEYS["organization"]["website"]) or ""
        if isinstance(website_url, list):
            # Pipedrive sometimes returns website as a list of dicts
            website_url = website_url[0].get("value", "") if website_url else ""
        if website_url:
            extra_context["website_text"] = fetch_website_text(website_url)

        # 2. LinkedIn URL (passed to AI as a hint; scraping blocked by LinkedIn)
        linkedin_url = data.get(FIELD_KEYS["organization"]["linkedin"], "")
        extra_context["linkedin_url"] = linkedin_url  # used in prompt

    # ── Generate AI summary ─────────────────────────────────────────────────
    try:
        ai_text = ai_write_summary(resource, data, extra_context)
    except Exception as e:
        return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

    # ── Write back to Pipedrive ─────────────────────────────────────────────
    u = requests.put(put_url, json={target_field: ai_text}, headers=headers, timeout=30)
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update record", "body": u.text}, status_code=400)

    return {"ok": True, "message": "Done. Field populated successfully."}


# Static files (panel.html and any JS/CSS)
app.mount("/static", StaticFiles(directory="static"), name="static")