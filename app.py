import os
import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "https://pipedrive-button.onrender.com")
PIPEDRIVE_CLIENT_ID = os.getenv("PIPEDRIVE_CLIENT_ID", "")
REDIRECT_URI = f"{BASE_URL}/oauth/callback"


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
    # This is just wiring for now.
    # Next steps will:
    # 1) load stored OAuth token for companyId/userId
    # 2) fetch the record from Pipedrive
    # 3) check configured custom fields for emptiness
    # 4) call AI and update the empty fields
    resource = payload.get("resource")
    record_id = payload.get("id")

    return {
        "ok": True,
        "message": f"Wired! Next: fetch {resource} #{record_id}, detect empty fields, fill with AI, update Pipedrive."
    }

# Static files (panel.html and any JS/CSS if you add later)
app.mount("/static", StaticFiles(directory="static"), name="static")