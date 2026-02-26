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

# Custom field API keys
FIELD_KEYS = {
    "deal": {
        "deal_context": "e637e09d69529de9a304c5a82a7a16eccee68c83",
    },
    "person": {
        "linkedin":   "6c01a20f553c1d1ab860a7396a65f55667d623d8",
        "background": "ea9b03ac0608816c4dbe05a2ce5109ff8276aab8",
    },
    "organization": {
        "linkedin": "dce5d063616e3008d850b211ef4072181a02e02e",
        "website":  "website",
        "about":    "fd1f632d86b97eb74f18daadc8ea6d0afaf0f6a2",
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


def save_tokens(company_id: str, access_token: str, refresh_token: str, expires_in: int):
    expires_at = int(time.time()) + int(expires_in) - 60
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO tokens(company_id, access_token, refresh_token, expires_at) "
        "VALUES (?, ?, ?, ?)",
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


def save_oauth_state(state: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO oauth_states(state, created_at) VALUES (?, ?)",
        (state, int(time.time())),
    )
    conn.commit()
    conn.close()


def consume_oauth_state(state: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (int(time.time()) - 600,))
    row = conn.execute("SELECT state FROM oauth_states WHERE state=?", (state,)).fetchone()
    if row:
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    conn.commit()
    conn.close()
    return row is not None


# ──────────────────────────────────────────────────────────────────────────────
# Website scraping (for organisation fallback)
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

    return " ".join("".join(text).split())[:8000]


# ──────────────────────────────────────────────────────────────────────────────
# AI summary — uses OpenAI web search to read LinkedIn directly
# ──────────────────────────────────────────────────────────────────────────────

def ai_write_person_summary(name: str, org: str, linkedin_url: str) -> str:
    """
    Uses OpenAI's web_search_preview tool so the model can browse the
    LinkedIn profile just like ChatGPT does when you paste a URL in chat.
    """
    prompt = f"""
Please look up this LinkedIn profile and write a professional CRM background summary.

LinkedIn URL: {linkedin_url}

The person's name is {name} and they are associated with {org}.

Using the information you find on that LinkedIn page, write a 4-6 sentence
professional background covering:
- Their current role and company
- Previous career highlights
- Education
- Key skills, languages, or expertise areas
- Any notable interests or volunteer work if present

Rules:
- Use only real facts from the LinkedIn page. Do not invent anything.
- Do NOT mention LinkedIn, URLs, or say "according to their profile".
- Do NOT say information is unavailable — only include what you actually found.
- Write in third person, plain text only, no bullet points.
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        input=[
            {
                "role": "system",
                "content": "You are a precise CRM assistant. You write factual, specific summaries strictly from data you find. You never invent or use filler phrases.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    return resp.output_text.strip()


def ai_write_org_summary(name: str, linkedin_url: str, website_url: str, website_text: str) -> str:
    """
    Uses OpenAI's web_search_preview tool to browse the company LinkedIn
    page and/or website and write a meaningful About paragraph.
    """
    sources = []
    if linkedin_url:
        sources.append(f"LinkedIn: {linkedin_url}")
    if website_url:
        sources.append(f"Website: {website_url}")

    extra = ""
    if website_text:
        extra = f"\nHere is the scraped text from their website to help you:\n{website_text}\n"

    prompt = f"""
Please look up information about this company and write a CRM "About" summary.

Company name: {name}
{chr(10).join(sources)}
{extra}

Write a 4-6 sentence description covering:
- What the company does / its core business
- Industry and market
- Size or reach if available
- Location / headquarters
- Any notable specialities, products, or clients

Rules:
- Use only real facts you find. Do not invent anything.
- Do NOT mention LinkedIn, websites, or URLs in the output.
- Do NOT say information is unavailable — only include what you actually found.
- Plain text only, no bullet points.
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        input=[
            {
                "role": "system",
                "content": "You are a precise CRM assistant. You write factual, specific summaries strictly from data you find. You never invent or use filler phrases.",
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
    org      = (
        (record.get("org_id") or {}).get("name")
        if isinstance(record.get("org_id"), dict)
        else record.get("org_id") or ""
    )
    person   = (
        (record.get("person_id") or {}).get("name")
        if isinstance(record.get("person_id"), dict)
        else record.get("person_id") or ""
    )

    prompt = f"""
Write a short deal context note (3-5 sentences) useful before a sales call or email.
Use only the data provided. Do not invent anything. Plain text only.

Deal title: {title}
Value: {value} {currency}
Stage: {stage}
Organisation: {org}
Primary contact: {person}
""".strip()

    resp = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "system",
                "content": "You are a precise CRM assistant. Write factual, useful deal context notes.",
            },
            {"role": "user", "content": prompt},
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
        return JSONResponse(
            {"error": "Invalid or expired state — please start again via /oauth/start."},
            status_code=400,
        )
    if not code:
        return JSONResponse(
            {"error": "No authorisation code returned. User may have declined."},
            status_code=400,
        )

    client_secret = os.getenv("PIPEDRIVE_CLIENT_SECRET", "")
    if not PIPEDRIVE_CLIENT_ID or not client_secret:
        return JSONResponse(
            {"error": "Missing PIPEDRIVE_CLIENT_ID or PIPEDRIVE_CLIENT_SECRET env var"},
            status_code=500,
        )

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
        return JSONResponse(
            {"error": "Token exchange failed", "status": r.status_code, "body": r.text},
            status_code=400,
        )

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

    return {
        "ok":                True,
        "company_id":        company_id,
        "got_access_token":  True,
        "got_refresh_token": True,
        "expires_in":        expires_in,
        "note":              "OAuth complete. Tokens saved successfully.",
    }


@app.post("/api/populate")
async def api_populate(payload: dict):
    resource   = payload.get("resource")
    record_id  = str(payload.get("id"))
    company_id = str(payload.get("companyId"))

    if resource not in ("deal", "person", "organization"):
        return JSONResponse({"error": "Unsupported resource"}, status_code=400)

    tokens = load_tokens(company_id)
    if not tokens:
        return JSONResponse({"error": "Not connected. Run /oauth/start once."}, status_code=401)

    access_token = tokens["access_token"]
    headers      = {"Authorization": f"Bearer {access_token}"}
    base         = "https://api.pipedrive.com/v1"

    if resource == "deal":
        get_url      = f"{base}/deals/{record_id}"
        put_url      = f"{base}/deals/{record_id}"
        target_field = FIELD_KEYS["deal"]["deal_context"]
    elif resource == "person":
        get_url      = f"{base}/persons/{record_id}"
        put_url      = f"{base}/persons/{record_id}"
        target_field = FIELD_KEYS["person"]["background"]
    else:
        get_url      = f"{base}/organizations/{record_id}"
        put_url      = f"{base}/organizations/{record_id}"
        target_field = FIELD_KEYS["organization"]["about"]

    # Fetch record from Pipedrive
    r = requests.get(get_url, headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Failed to fetch record", "body": r.text}, status_code=400)

    data = r.json().get("data", {})

    # Skip if already filled
    if data.get(target_field) not in (None, "", []):
        return {"ok": True, "message": "Field already filled. Nothing to do."}

    # ── Generate summary ─────────────────────────────────────────────────────
    try:
        if resource == "person":
            linkedin_url = data.get(FIELD_KEYS["person"]["linkedin"], "")
            if not linkedin_url:
                return JSONResponse(
                    {"error": "No LinkedIn URL on this person record. Please add one first."},
                    status_code=400,
                )
            name = data.get("name", "")
            org  = (
                (data.get("org_id") or {}).get("name")
                if isinstance(data.get("org_id"), dict)
                else data.get("org_id") or ""
            )
            ai_text = ai_write_person_summary(name, org, linkedin_url)

        elif resource == "organization":
            linkedin_url = data.get(FIELD_KEYS["organization"]["linkedin"], "")
            website_url  = data.get("website") or ""
            if isinstance(website_url, list):
                website_url = website_url[0].get("value", "") if website_url else ""

            if not linkedin_url and not website_url:
                return JSONResponse(
                    {"error": "No LinkedIn URL or website on this organisation. Please add at least one."},
                    status_code=400,
                )

            website_text = fetch_website_text(website_url) if website_url else ""
            ai_text = ai_write_org_summary(
                name=data.get("name", ""),
                linkedin_url=linkedin_url,
                website_url=website_url,
                website_text=website_text,
            )

        else:  # deal
            ai_text = ai_write_deal_summary(data)

    except Exception as e:
        return JSONResponse({"error": "AI generation failed", "details": str(e)}, status_code=500)

    # Write back to Pipedrive
    u = requests.put(put_url, json={target_field: ai_text}, headers=headers, timeout=30)
    if u.status_code != 200:
        return JSONResponse({"error": "Failed to update record", "body": u.text}, status_code=400)

    return {"ok": True, "message": "Done. Field populated successfully."}


# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")