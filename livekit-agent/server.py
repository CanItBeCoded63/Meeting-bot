"""
server.py – FastAPI backend for the Voice Agent Web UI.

Endpoints:
    GET  /              → Serves the static HTML page
    GET  /api/projects  → Returns available projects from projects_config.json
    POST /api/token     → Generates a LiveKit room token with provider metadata

Run:
    uv run python server.py
"""

import os
import json
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(".env")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from livekit import api

# ── Config ────────────────────────────────────────────────────────────────────
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
CONFIG_FILE = Path(__file__).parent / "projects_config.json"

app = FastAPI(title="Voice Agent UI")


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects():
    """Return all projects from projects_config.json."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return JSONResponse(data)
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid config file"}, status_code=500)
    return JSONResponse({})


@app.post("/api/token")
async def create_token(request: Request):
    """
    Generate a LiveKit room token.

    Expects JSON body:
    {
        "stt_provider": "sarvam" | "deepgram",
        "stt_model": "saaras:v3" | "nova-2" | ...,
        "tts_provider": "sarvam" | "deepgram",
        "tts_model": "bulbul:v3" | "aura-asteria-en" | ...,
        "tts_speaker": "shubh" | null
    }

    Returns:
    {
        "token": "<jwt>",
        "room": "<room-name>",
        "ws_url": "<livekit-url>"
    }
    """
    body = await request.json()

    stt_provider = body.get("stt_provider", "sarvam")
    stt_model = body.get("stt_model", "saaras:v3")
    tts_provider = body.get("tts_provider", "sarvam")
    tts_model = body.get("tts_model", "bulbul:v3")
    tts_speaker = body.get("tts_speaker", "shubh")

    # Create a unique room name for this session
    room_name = f"voice-{uuid.uuid4().hex[:10]}"

    # Build room metadata – the agent will read this on join
    metadata = json.dumps({
        "stt_provider": stt_provider,
        "stt_model": stt_model,
        "tts_provider": tts_provider,
        "tts_model": tts_model,
        "tts_speaker": tts_speaker,
        "source": "web-ui",
    })

    # Generate the access token
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(f"web-user-{uuid.uuid4().hex[:6]}")
        .with_name("Web User")
        .with_metadata(metadata)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
            )
        )
        .to_jwt()
    )

    return JSONResponse({
        "token": token,
        "room": room_name,
        "ws_url": LIVEKIT_URL,
    })


# ── Static Files (serve index.html) ──────────────────────────────────────────

# Serve the static directory for CSS/JS assets
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main HTML page."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found in static/</h1>", status_code=404)


# Mount static files for any additional assets
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("\n  [*] Voice Agent Web UI")
    print(f"  [*] LiveKit URL: {LIVEKIT_URL}")
    print(f"  [*] Open http://localhost:8000 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
