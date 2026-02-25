import os
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "https://pipedrive-button.onrender.com")
PIPEDRIVE_CLIENT_ID = os.getenv("PIPEDRIVE_CLIENT_ID", "")
PIPEDRIVE_REDIRECT_URI = f"{BASE_URL}/oauth/callback"

# Serve frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/health")
def health():
    return {"status": "ok"}

# 1) Start OAuth (you'll click this during testing)
@app.get("/oauth/start")
def oauth_start():
    if not PIPEDRIVE_CLIENT_ID:
        return JSONResponse({"error": "Missing PIPEDRIVE_CLIENT_ID env var"}, status_code=500)

    auth_url = (
        "https://oauth.pipedrive.com/oauth/authorize"
        f"?client_id={PIPEDRIVE_CLIENT_ID}"
        f"&redirect_uri={PIPEDRIVE_REDIRECT_URI}"
        f"&response_type=code"
        f"&state=demo"
    )
    return RedirectResponse(auth_url)

# 2) OAuth callback (Pipedrive redirects here with ?code=...)
@app.get("/oauth/callback")
def oauth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    # For now, just show that it worked. Next step we'll exchange code for tokens.
    return {
        "received_code": bool(code),
        "code_preview": (code[:6] + "...") if code else None,
        "state": state,
        "next": "Next step: exchange code for access_token using client_secret"
    }