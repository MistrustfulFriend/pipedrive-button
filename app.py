import os
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import requests  # add at top if not present

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "https://pipedrive-button.onrender.com")
PIPEDRIVE_CLIENT_ID = os.getenv("PIPEDRIVE_CLIENT_ID", "")
REDIRECT_URI = f"{BASE_URL}/oauth/callback"

# ---- API routes first ----

@app.get("/panel")
def panel():
    return FileResponse("static/panel.html")

@app.get("/action")
def action_page():
    return FileResponse("static/action.html")

@app.get("/health")
def health():
    return {"status": "ok"}

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

    if not PIPEDRIVE_CLIENT_ID or not os.getenv("PIPEDRIVE_CLIENT_SECRET"):
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID/SECRET env vars"}, status_code=500)

    token_url = "https://oauth.pipedrive.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": PIPEDRIVE_CLIENT_ID,
        "client_secret": os.getenv("PIPEDRIVE_CLIENT_SECRET"),
    }

    r = requests.post(token_url, data=payload, timeout=30)
    if r.status_code != 200:
        return JSONResponse({"error": "Token exchange failed", "status": r.status_code, "body": r.text}, status_code=400)

    tokens = r.json()

    # For now, just confirm success without leaking tokens:
    return {
        "ok": True,
        "got_access_token": bool(tokens.get("access_token")),
        "got_refresh_token": bool(tokens.get("refresh_token")),
        "expires_in": tokens.get("expires_in"),
        "note": "Next step: store tokens securely per company/user"
    }

# ---- Frontend ----

@app.get("/")
def index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")