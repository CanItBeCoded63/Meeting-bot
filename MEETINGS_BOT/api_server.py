"""FastAPI server for launching Teams Audio Agent bots via REST API.

Usage:
    python api_server.py
    uvicorn api_server:app --host 0.0.0.0 --port 8000

Endpoints:
    POST /bot/start   — Start a bot in a Teams meeting
    POST /bot/stop    — Stop the active bot session
    GET  /bot/status  — Get current bot status
"""

import asyncio
import logging
import os
import subprocess
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.teams_agent.__main__ import BotSession
from src.teams_agent.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("teams_agent.api")

app = FastAPI(
    title="Teams Audio Agent API",
    description="Launch AI voice bots into Microsoft Teams meetings",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DigitalClone Bot Control Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #090d16;
            --bg-surface: rgba(17, 24, 39, 0.7);
            --border-glow: rgba(99, 102, 241, 0.2);
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --success: #10b981;
            --danger: #ef4444;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
        }

        body {
            background-color: var(--bg-base);
            background-image: 
                radial-gradient(at 10% 10%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 90% 90%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 2rem;
        }

        .container {
            width: 100%;
            max-width: 600px;
            background: var(--bg-surface);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-glow);
            border-radius: 24px;
            padding: 2.5rem;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
            transition: border-color 0.3s ease;
        }

        .header {
            text-align: center;
            margin-bottom: 2rem;
        }

        .header h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #a78bfa, #818cf8, #34d399);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }

        .header p {
            color: var(--text-muted);
            font-size: 0.95rem;
        }

        .status-card {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 1.25rem 1.5rem;
            margin-bottom: 2rem;
        }

        .status-info {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background-color: var(--text-muted);
            box-shadow: 0 0 10px var(--text-muted);
            transition: all 0.3s ease;
        }

        .status-indicator.idle {
            background-color: #f59e0b;
            box-shadow: 0 0 12px #f59e0b;
        }

        .status-indicator.starting {
            background-color: #6366f1;
            box-shadow: 0 0 12px #6366f1;
            animation: pulse 1.5s infinite;
        }

        .status-indicator.joined, .status-indicator.ready, .status-indicator.running {
            background-color: var(--success);
            box-shadow: 0 0 12px var(--success);
        }

        .status-indicator.stopped {
            background-color: var(--danger);
            box-shadow: 0 0 12px var(--danger);
        }

        .status-text {
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.9rem;
            letter-spacing: 0.05em;
        }

        .form-group {
            margin-bottom: 1.5rem;
        }

        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            color: var(--text-muted);
            font-size: 0.85rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .form-input {
            width: 100%;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 12px;
            padding: 0.85rem 1rem;
            color: var(--text-main);
            font-size: 0.95rem;
            transition: all 0.2s ease;
        }

        .form-input:focus {
            outline: none;
            border-color: var(--primary);
            background: rgba(255, 255, 255, 0.07);
            box-shadow: 0 0 15px rgba(99, 102, 241, 0.15);
        }

        .toggle-group {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            padding: 0.85rem 1rem;
            margin-bottom: 2rem;
            border: 1px solid rgba(255, 255, 255, 0.04);
        }

        .toggle-label {
            font-size: 0.9rem;
            font-weight: 500;
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 44px;
            height: 24px;
        }

        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(255, 255, 255, 0.1);
            transition: .3s;
            border-radius: 24px;
        }

        .slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }

        input:checked + .slider {
            background-color: var(--success);
        }

        input:checked + .slider:before {
            transform: translateX(20px);
        }

        .actions {
            display: flex;
            gap: 1rem;
        }

        .btn {
            flex: 1;
            padding: 1rem 1.5rem;
            border-radius: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            border: none;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
            font-size: 1rem;
        }

        .btn-primary {
            background: var(--primary);
            color: white;
            box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4);
        }

        .btn-primary:hover:not(:disabled) {
            background: var(--primary-hover);
            transform: translateY(-2px);
        }

        .btn-secondary {
            background: rgba(239, 68, 68, 0.15);
            color: var(--danger);
            border: 1px solid rgba(239, 68, 68, 0.2);
        }

        .btn-secondary:hover:not(:disabled) {
            background: rgba(239, 68, 68, 0.25);
            transform: translateY(-2px);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none !important;
        }

        .terminal {
            margin-top: 2rem;
            background: #020617;
            border-radius: 12px;
            padding: 1rem;
            border: 1px solid rgba(255, 255, 255, 0.05);
            font-family: 'Courier New', Courier, monospace;
            font-size: 0.8rem;
            color: #10b981;
            max-height: 120px;
            overflow-y: auto;
            white-space: pre-wrap;
        }

        @keyframes pulse {
            0% {
                transform: scale(0.95);
                box-shadow: 0 0 0 0 rgba(99, 102, 241, 0.7);
            }
            70% {
                transform: scale(1);
                box-shadow: 0 0 0 10px rgba(99, 102, 241, 0);
            }
            100% {
                transform: scale(0.95);
                box-shadow: 0 0 0 0 rgba(99, 102, 241, 0);
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>DigitalClone Control</h1>
            <p>Teams Voice Agent Session Orchestrator</p>
        </div>

        <div class="status-card">
            <div class="status-info">
                <div id="indicator" class="status-indicator"></div>
                <span id="statusLabel" class="status-text">Checking Status...</span>
            </div>
            <span id="sessionLabel" style="font-size: 0.8rem; color: var(--text-muted);">No Active Session</span>
        </div>

        <div class="form-group">
            <label for="meetingUrl">Teams Meeting URL</label>
            <input type="text" id="meetingUrl" class="form-input" placeholder="https://teams.live.com/meet/..." value="">
        </div>

        <div class="form-group">
            <label for="botName">Bot Display Name</label>
            <input type="text" id="botName" class="form-input" placeholder="AI Assistant" value="AI Assistant">
        </div>

        <div class="toggle-group">
            <span class="toggle-label">Enable Visual Awareness (Gemini Vision)</span>
            <label class="switch">
                <input type="checkbox" id="visionEnabled" checked>
                <span class="slider"></span>
            </label>
        </div>

        <div class="actions">
            <button id="startBtn" class="btn btn-primary" onclick="startBot()">
                Start Bot
            </button>
            <button id="stopBtn" class="btn btn-secondary" onclick="stopBot()" disabled>
                Stop Bot
            </button>
        </div>

        <div id="terminal" class="terminal">System initialized. Ready to start bot...</div>
    </div>

    <script>
        const statusLabel = document.getElementById('statusLabel');
        const indicator = document.getElementById('indicator');
        const sessionLabel = document.getElementById('sessionLabel');
        const startBtn = document.getElementById('startBtn');
        const stopBtn = document.getElementById('stopBtn');
        const meetingUrlInput = document.getElementById('meetingUrl');
        const botNameInput = document.getElementById('botName');
        const visionCheckbox = document.getElementById('visionEnabled');
        const terminal = document.getElementById('terminal');

        function logToTerminal(msg) {
            const time = new Date().toLocaleTimeString();
            terminal.innerText = `[${time}] ${msg}\\n` + terminal.innerText;
        }

        async function updateStatus() {
            try {
                const response = await fetch('/bot/status');
                const data = await response.json();
                
                statusLabel.innerText = data.status;
                indicator.className = 'status-indicator ' + data.status.toLowerCase();
                
                if (data.session_id) {
                    sessionLabel.innerText = `Session: ${data.session_id}`;
                } else {
                    sessionLabel.innerText = 'No Active Session';
                }

                if (data.status === 'idle' || data.status === 'stopped') {
                    startBtn.disabled = false;
                    stopBtn.disabled = true;
                    meetingUrlInput.disabled = false;
                    botNameInput.disabled = false;
                } else {
                    startBtn.disabled = true;
                    stopBtn.disabled = false;
                    meetingUrlInput.disabled = true;
                    botNameInput.disabled = true;
                }
            } catch (err) {
                console.error("Failed to fetch status:", err);
            }
        }

        async function startBot() {
            const meeting_url = meetingUrlInput.value.trim();
            const bot_name = botNameInput.value.trim();
            const vision_enabled = visionCheckbox.checked;

            if (!meeting_url) {
                alert("Please enter a Teams Meeting URL.");
                return;
            }

            logToTerminal(`Initiating join request for "${bot_name}"...`);
            startBtn.disabled = true;

            try {
                const response = await fetch('/bot/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ meeting_url, bot_name, vision_enabled })
                });
                const data = await response.json();
                
                if (response.ok) {
                    logToTerminal(`Bot started successfully! ID: ${data.session_id}`);
                } else {
                    logToTerminal(`Start failed: ${data.detail || 'Unknown error'}`);
                }
                updateStatus();
            } catch (err) {
                logToTerminal(`Request error: ${err.message}`);
                updateStatus();
            }
        }

        async function stopBot() {
            logToTerminal("Sending stop request...");
            stopBtn.disabled = true;

            try {
                const response = await fetch('/bot/stop', { method: 'POST' });
                const data = await response.json();
                
                if (response.ok) {
                    logToTerminal("Bot stopped successfully.");
                } else {
                    logToTerminal(`Stop failed: ${data.detail || 'Unknown error'}`);
                }
                updateStatus();
            } catch (err) {
                logToTerminal(`Request error: ${err.message}`);
                updateStatus();
            }
        }

        // Initialize status polling
        updateStatus();
        setInterval(updateStatus, 2000);
    </script>
</body>
</html>"""

# Single active session (one bot at a time on this server)
_session: BotSession | None = None
_session_id: str | None = None
_session_task: asyncio.Task | None = None


class StartRequest(BaseModel):
    meeting_url: str
    bot_name: str = "AI Assistant"
    vision_enabled: bool | None = None  # None = use .env VISION_ENABLED


class VisionRequest(BaseModel):
    enabled: bool


class StartResponse(BaseModel):
    session_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    session_id: str | None
    status: str
    message: str
    vision_active: bool = False


def _kill_port_holders(port: int):
    """Kill any process listening on the given TCP port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-aon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0 and pid != os.getpid():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                    )
                    logger.info("Killed process %d holding port %d", pid, port)
    except Exception as e:
        logger.warning("Failed to kill port %d holders: %s", port, e)


async def _cleanup_previous_session():
    """Force-stop any existing bot session and release OS resources."""
    global _session, _session_id, _session_task

    if _session:
        logger.info("Cleaning up previous session (status=%s)...", _session.status)
        _session.shutdown_event.set()

        if _session_task and not _session_task.done():
            _session_task.cancel()
            try:
                await asyncio.wait_for(_session_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        _session = None
        _session_id = None
        _session_task = None

    # Kill anything still holding the WSS port
    cfg = Config()
    _kill_port_holders(cfg.WS_PORT)
    # Brief pause to let the OS release the socket
    await asyncio.sleep(1)


@app.post("/bot/start", response_model=StartResponse)
async def start_bot(req: StartRequest):
    """Start a bot session in the given Teams meeting.

    Returns once the bot has joined the meeting, audio pipeline is active,
    and the bot is ready to listen and respond.
    """
    global _session, _session_id, _session_task

    # Force-cleanup any previous session (crashed or still running)
    await _cleanup_previous_session()

    _session = BotSession()
    _session_id = uuid.uuid4().hex[:12]
    ready_event = asyncio.Event()

    async def on_ready():
        ready_event.set()

    async def run_session():
        try:
            await _session.start(
                meeting_url=req.meeting_url,
                bot_name=req.bot_name,
                ready_callback=on_ready,
                vision_enabled=req.vision_enabled,
            )
        except Exception:
            logger.exception("Bot session crashed")
            _session.status = "stopped"

    # Launch session in background
    _session_task = asyncio.create_task(run_session())

    # Wait for the bot to be fully ready (joined + pipeline + audio)
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Bot failed to become ready within 60s (status={_session.status})",
        )

    return StartResponse(
        session_id=_session_id,
        status=_session.status,
        message=f"Bot '{req.bot_name}' joined and ready in meeting",
    )


@app.post("/bot/stop", response_model=StatusResponse)
async def stop_bot():
    """Stop the active bot session."""
    if not _session or _session.status == "stopped":
        raise HTTPException(status_code=404, detail="No active bot session")

    await _session.stop()
    # Give it a moment to clean up
    if _session_task:
        try:
            await asyncio.wait_for(_session_task, timeout=10)
        except asyncio.TimeoutError:
            pass

    return StatusResponse(
        session_id=_session_id,
        status="stopped",
        message="Bot session stopped",
        vision_active=False,
    )


@app.get("/bot/status", response_model=StatusResponse)
async def get_status():
    """Get the current bot session status."""
    if not _session:
        return StatusResponse(
            session_id=None,
            status="idle",
            message="No bot session",
        )

    return StatusResponse(
        session_id=_session_id,
        status=_session.status,
        message=f"Bot session {_session_id}",
        vision_active=_session.vision_active,
    )


@app.post("/bot/vision", response_model=StatusResponse)
async def toggle_vision(req: VisionRequest):
    """Enable or disable vision sharing on the active bot session."""
    if not _session or _session.status not in ("ready", "running"):
        raise HTTPException(status_code=404, detail="No active bot session")

    if req.enabled:
        success = await _session.start_vision()
        msg = "Vision observer started" if success else "Failed to start vision observer"
    else:
        success = await _session.stop_vision()
        msg = "Vision observer stopped" if success else "Failed to stop vision observer"

    if not success:
        raise HTTPException(status_code=500, detail=msg)

    return StatusResponse(
        session_id=_session_id,
        status=_session.status,
        message=msg,
        vision_active=_session.vision_active,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=6789)
