import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve the frontend (HTML/JS) from / (root)
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# Quick health check (useful for debugging)
@app.get("/health")
def health():
    return {"status": "ok"}