import os
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

FIELD_KEYS = {
    "deal": {
        "deal_context": "e637e09d69529de9a304c5a82a7a16eccee68c83",
    },
    "organization": {
        "about": "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2",  # About target field
    },
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
# Website scraper
# ──────────────────────────────────────────────────────────────────────────────

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

    return " ".join("".join(text).split())[:8000]


# ──────────────────────────────────────────────────────────────────────────────
# AI summaries
# ──────────────────────────────────────────────────────────────────────────────

def ai_write_org_summary(name: str, website_url: str, website_text: str) -> str:
    prompt = f"""
Below is text scraped directly from this company's website.
Write a CRM "About" summary strictly based on this text.

Company name: {name}
Website: {website_url}

== WEBSITE TEXT (your only source) ==
{website_text}
== END ==

Write 4-6 sentences covering:
- What the company does / core business
- Industry
- Size or reach if mentioned
- Location / headquarters if mentioned
- Notable specialities, products, or clients if mentioned

STRICT RULES:
- Use ONLY facts that appear in the source text above. Do not invent anything.
- Do NOT mention the website URL or say "according to their website".
- Do NOT say "information is unavailable" — just omit what you don't have.
- Plain text only, no bullet points, no headers.
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "system",
                "content": "You are a precise CRM assistant. You write factual summaries strictly from provided source text. You never invent or pad.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    return resp.output_text.strip()


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
            {
                "role": "system",
                "content": "You write short factual CRM deal context notes. Never invent anything.",
            },
            {
                "role": "user",
                "content": (
                    f"Write a 3-5 sentence deal context note useful before a sales call. Plain text only.\n\n"
                    f"Deal: {title}\nValue: {value} {currency}\nStage: {stage}\nOrg: {org}\nContact: {person}"
                ),
            },
        ],
    )
    return resp.output_text.strip()


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

    # Person enrichment is not supported — nothing to do
    if resource == "person":
        return JSONResponse(
            {"error": "Person enrichment is not available. Only Organisation and Deal fields can be populated."},
            status_code=400,
        )

    tokens = load_tokens(company_id)
    if not tokens:
        return JSONResponse({"error": "Not connected. Run /oauth/start once."}, status_code=401)

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    base    = "https://api.pipedrive.com/v1"

    if resource == "deal":
        get_url      = f"{base}/deals/{record_id}"
        put_url      = f"{base}/deals/{record_id}"
        target_field = FIELD_KEYS["deal"]["deal_context"]
    else:  # organization
        get_url      = f"{base}/organizations/{record_id}"
        put_url      = f"{base}/organizations/{record_id}"
        target_field = FIELD_KEYS["organization"]["about"]

    r = requests.get(get_url, headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch record", "body": r.text}, status_code=400)

    data = r.json().get("data", {})

    # Skip if already filled
    if data.get(target_field) not in (None, "", []):
        return {"ok": True, "message": "Field already filled. Nothing to do."}

    # Generate summary
    try:
        if resource == "organization":
            website_url = data.get("website") or ""
            if isinstance(website_url, list):
                website_url = website_url[0].get("value", "") if website_url else ""

            if not website_url:
                return JSONResponse(
                    {"error": "No website found on this organisation record. Please add one first."},
                    status_code=400,
                )

            website_text = fetch_website_text(website_url)
            if not website_text or len(website_text) < 100:
                return JSONResponse(
                    {"error": f"Could not read content from {website_url}. The site may be blocking requests or require JavaScript."},
                    status_code=400,
                )

            ai_text = ai_write_org_summary(data.get("name", ""), website_url, website_text)

        else:  # deal
            ai_text = ai_write_deal_summary(data)

    except Exception as e:
        return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

    u = requests.put(put_url, json={target_field: ai_text}, headers=headers, timeout=30)
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update record", "body": u.text}, status_code=400)

    return {"ok": True, "message": "Done. Field populated successfully."}


app.mount("/static", StaticFiles(directory="static"), name="static")