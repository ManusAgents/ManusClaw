from __future__ import annotations

"""
ManusClaw HTTP/WebSocket Server
================================
FastAPI backend that exposes the full ManusClaw agent engine via:
  • REST API   — session management, history queries, tool introspection
  • WebSocket  — real-time streaming of agent thoughts, tool calls, and outputs
  • API Key    — optional authentication via MANUSCLAW_API_KEY env var
  • CORS       — configurable via MANUSCLAW_ALLOWED_ORIGINS env var

Run with:
  python run_server.py
  # or
  uvicorn app.server.main:app --host 0.0.0.0 --port 8765 --reload
"""

import asyncio
import json
import os
import time
from typing import Any, Optional

from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db.session import SessionDB
from app.logger import logger
from app.permissions.gate import AgentMode

@asynccontextmanager
async def _lifespan(application: FastAPI):
    """FIX: Startup/shutdown lifecycle — log warnings at startup, clean up on shutdown."""
    if not _API_KEY:
        logger.warning(
            "MANUSCLAW_API_KEY not set — all endpoints are UNAUTHENTICATED. "
            "Set MANUSCLAW_API_KEY in production."
        )
    logger.info("ManusClaw Agent Server v4.0 started.")
    application.state.background_tasks = set()
    yield
    # Cleanup: cancel any still-running background agent tasks
    bg = getattr(application.state, "background_tasks", set())
    if bg:
        logger.info(f"[Server] Cancelling {len(bg)} background task(s) on shutdown.")
        for t in list(bg):
            t.cancel()
        import asyncio as _asyncio
        await _asyncio.gather(*bg, return_exceptions=True)
    logger.info("ManusClaw Agent Server shut down cleanly.")

app = FastAPI(
    title="ManusClaw Agent Server",
    description="Autonomous AI agent engine by The-JDdev (SHS Shobuj)",
    version="4.0.0",
    lifespan=_lifespan,
)

_raw_origins = os.getenv("MANUSCLAW_ALLOWED_ORIGINS", "")
_allowed_origins: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else []
)

if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Fix: warn about open CORS in production
    logger.warning(
        "MANUSCLAW_ALLOWED_ORIGINS not set — CORS allows all origins. "
        "Set MANUSCLAW_ALLOWED_ORIGINS in production to restrict access."
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

_API_KEY = os.getenv("MANUSCLAW_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Static files directory
_STATIC_DIR = Path(__file__).parent / "static"

# API key authentication


async def require_api_key(key: Optional[str] = Depends(_api_key_header)) -> None:
    if not _API_KEY:
        return
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


class ConnectionManager:
    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        self.active[session_id] = ws

    def disconnect(self, session_id: str) -> None:
        self.active.pop(session_id, None)

    async def send(self, session_id: str, data: dict) -> None:
        ws = self.active.get(session_id)
        if ws:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                self.disconnect(session_id)

    async def broadcast(self, data: dict) -> None:
        for sid, ws in list(self.active.items()):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                self.disconnect(sid)


manager = ConnectionManager()
db = SessionDB()

# ─── Canvas & Chat integration ──────────────────────────────────────────

# Canvas chat connection manager (multiple clients per session)
canvas_chat_manager: dict[str, list[WebSocket]] = {}

# Lazy-init canvas server
def _get_canvas_server():
    from app.canvas.server import CanvasServer
    srv = getattr(app.state, "canvas_server", None)
    if srv is None:
        srv = CanvasServer()
        app.state.canvas_server = srv
    return srv



class StreamingManus:
    """Wraps Manus agent with WebSocket event emission and unified session tracking."""

    def __init__(self, session_id: str, mode: AgentMode = AgentMode.BUILD, max_steps: Optional[int] = None) -> None:
        self.session_id = session_id
        self.mode = mode
        self.max_steps = max_steps

    async def run(self, prompt: str) -> str:
        from app.agent.manus import Manus

        agent = Manus(mode=self.mode, session_id=self.session_id)
        if self.max_steps is not None:
            agent._max_steps = self.max_steps

        original_step = agent.step

        async def patched_step():
            # FIX: capture current step count BEFORE calling original_step
            # (original_step increments _step_count at the end)
            step_num = agent._step_count + 1
            await manager.send(self.session_id, {
                "type": "step_start",
                "step": step_num,
                "ts": time.time(),
            })
            result = await original_step()
            if result:
                await manager.send(self.session_id, {
                    "type": "step_output",
                    "step": step_num,
                    "content": result[:2000],
                    "ts": time.time(),
                })
            return result

        agent.step = patched_step  # type: ignore

        try:
            final = await agent.run(prompt)
            await manager.send(self.session_id, {
                "type": "agent_done",
                "output": final[:4000],
                "state": agent.state.value,
                "ts": time.time(),
            })
            return final
        except Exception as e:
            await manager.send(self.session_id, {
                "type": "agent_error",
                "error": str(e),
                "ts": time.time(),
            })
            raise


class RunRequest(BaseModel):
    prompt: str
    mode: str = "build"
    max_steps: int = 30


class RunResponse(BaseModel):
    session_id: str
    status: str
    output: Optional[str] = None


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "4.0.0", "agent": "ManusClaw"}


@app.get("/")
async def root():
    return {"message": "ManusClaw Agent Server v4.0 — connect via /ws/<session_id>"}


@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_key)])
async def run_agent(req: RunRequest):
    mode = AgentMode.PLAN if req.mode.lower() == "plan" else AgentMode.BUILD
    mode_str = mode.value
    session_id = await db.create_session(req.prompt, mode=mode_str)  # Fix: use enum value

    async def _run():
        streamer = StreamingManus(session_id=session_id, mode=mode, max_steps=req.max_steps)
        try:
            await streamer.run(req.prompt)
        except Exception as e:
            logger.error(f"[Server] Agent run error: {e}")

    # Fix: store task reference to prevent GC and lost errors
    task = asyncio.create_task(_run())
    _bg = getattr(app.state, 'background_tasks', set())
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    app.state.background_tasks = _bg
    return RunResponse(session_id=session_id, status="running")


@app.post("/run/sync", response_model=RunResponse, dependencies=[Depends(require_api_key)])
async def run_agent_sync(req: RunRequest):
    from app.agent.manus import Manus
    mode = AgentMode.PLAN if req.mode.lower() == "plan" else AgentMode.BUILD
    mode_str = mode.value
    session_id = await db.create_session(req.prompt, mode=mode_str)  # Fix: use enum value
    try:
        agent = Manus(mode=mode, session_id=session_id)
        agent._max_steps = req.max_steps
        output = await agent.run(req.prompt)
        await db.close_session(session_id, state="finished")
        return RunResponse(session_id=session_id, status="finished", output=output)
    except Exception as e:
        await db.close_session(session_id, state="error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions", dependencies=[Depends(require_api_key)])
async def list_sessions(limit: int = 20):
    sessions = await db.get_sessions(limit=limit)
    return {"sessions": sessions}


@app.get("/sessions/{session_id}/messages", dependencies=[Depends(require_api_key)])
async def get_messages(session_id: str):
    msgs = await db.get_session_messages(session_id)
    return {"session_id": session_id, "messages": msgs}


@app.get("/sessions/{session_id}/tool_calls", dependencies=[Depends(require_api_key)])
async def get_tool_calls(session_id: str):
    calls = await db.get_session_tool_calls(session_id)
    return {"session_id": session_id, "tool_calls": calls}


@app.get("/tools")
async def list_tools():
    from app.tool.base import ToolCollection
    from app.tool.python_execute import PythonExecute
    from app.tool.bash import Bash
    from app.tool.web_search import WebSearch
    from app.tool.str_replace_editor import StrReplaceEditor
    from app.tool.terminate import Terminate
    # FIX: cleanup() must be called to terminate the Bash persistent subprocess
    tools = ToolCollection(PythonExecute(), Bash(), WebSearch(), StrReplaceEditor(), Terminate())
    try:
        schemas = tools.to_openai_schemas()
        return {"tools": [{"name": s["function"]["name"], "description": s["function"]["description"]} for s in schemas]}
    finally:
        await tools.cleanup_all()


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    if _API_KEY:
        token = (
            websocket.query_params.get("api_key")
            or websocket.headers.get("x-api-key", "")
        )
        if token != _API_KEY:
            await websocket.close(code=4001)
            return

    await manager.connect(websocket, session_id)
    logger.info(f"[Server] WebSocket connected: {session_id}")
    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "session_id": session_id,
            "message": "ManusClaw WebSocket ready. Send {\"prompt\": \"...\"} to start.",
        }))
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            prompt = msg.get("prompt", "").strip()
            if not prompt:
                await websocket.send_text(json.dumps({"type": "error", "message": "No prompt provided"}))
                continue

            mode_str = msg.get("mode", "build")
            mode = AgentMode.PLAN if mode_str == "plan" else AgentMode.BUILD

            await websocket.send_text(json.dumps({"type": "agent_start", "prompt": prompt[:200]}))

            streamer = StreamingManus(session_id=session_id, mode=mode, max_steps=msg.get("max_steps"))
            try:
                await streamer.run(prompt)
            except Exception as e:
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))

    except WebSocketDisconnect:
        manager.disconnect(session_id)
        logger.info(f"[Server] WebSocket disconnected: {session_id}")


class MultiAgentRequest(BaseModel):
    goal: str
    mode: str = "build"
    roles: Optional[list[str]] = None


@app.post("/multi-agent", dependencies=[Depends(require_api_key)])
async def run_multi_agent(req: MultiAgentRequest):
    from app.agent.orchestrator import MultiAgentOrchestrator
    mode = AgentMode.PLAN if req.mode.lower() == "plan" else AgentMode.BUILD
    orchestrator = MultiAgentOrchestrator(mode=mode)
    result = await orchestrator.run(req.goal)
    return {"result": result}


# ─── Static file serving & HTML pages ─────────────────────────────────────

# Mount static files (must come before catch-all routes)
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ─── Webhook router ─────────────────────────────────────────────────────────
from app.server.webhook_router import router as webhook_router
app.include_router(webhook_router)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    """Serve the built-in web chat interface."""
    chat_html = _STATIC_DIR / "chat.html"
    if chat_html.is_file():
        return HTMLResponse(content=chat_html.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>ManusClaw WebChat</h1><p>chat.html not found.</p>")


@app.get("/canvas", response_class=HTMLResponse)
async def canvas_page(session: str = "default"):
    """Serve the canvas viewer page."""
    canvas_html = _STATIC_DIR / "canvas.html"
    if canvas_html.is_file():
        return HTMLResponse(content=canvas_html.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>ManusClaw Canvas</h1><p>canvas.html not found.</p>")


# ─── Chat WebSocket endpoint ─────────────────────────────────────────────

async def _auth_ws(websocket: WebSocket) -> bool:
    """Check WebSocket authentication. Returns True if authorized."""
    if not _API_KEY:
        return True
    token = (
        websocket.query_params.get("api_key")
        or websocket.headers.get("x-api-key", "")
    )
    return token == _API_KEY


@app.websocket("/ws/chat/{session_id}")
async def chat_websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for the built-in web chat client.

    Similar to the generic /ws/{session_id} but with chat-specific message
    handling including typed prompts, file attachments, and canvas integration.
    """
    if not await _auth_ws(websocket):
        await websocket.close(code=4001)
        return

    await websocket.accept()

    # Register connection
    if session_id not in canvas_chat_manager:
        canvas_chat_manager[session_id] = []
    canvas_chat_manager[session_id].append(websocket)

    logger.info("[Server] Chat WebSocket connected: %s", session_id)

    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "session_id": session_id,
            "message": "ManusClaw Chat ready. Send {\"type\": \"prompt\", \"prompt\": \"...\"} to start.",
        }))
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            # Prompt message — run the agent
            prompt = msg.get("prompt", "").strip()
            if not prompt and msg_type != "prompt":
                # Unknown message type
                await websocket.send_text(json.dumps({"type": "error", "message": f"Unknown type: {msg_type}"}))
                continue

            if not prompt:
                await websocket.send_text(json.dumps({"type": "error", "message": "No prompt provided"}))
                continue

            mode_str = msg.get("mode", "build")
            mode = AgentMode.PLAN if mode_str == "plan" else AgentMode.BUILD

            await websocket.send_text(json.dumps({"type": "agent_start", "prompt": prompt[:200]}))

            streamer = StreamingManus(session_id=session_id, mode=mode, max_steps=msg.get("max_steps"))
            try:
                await streamer.run(prompt)
            except Exception as e:
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))

    except WebSocketDisconnect:
        logger.info("[Server] Chat WebSocket disconnected: %s", session_id)
    finally:
        conns = canvas_chat_manager.get(session_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            canvas_chat_manager.pop(session_id, None)


# ─── Canvas WebSocket endpoint ──────────────────────────────────────────

@app.websocket("/ws/canvas/{session_id}")
async def canvas_websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for the live canvas viewer.

    Handles A2UI protocol messages: sync, update, clear, event.
    Delegates to CanvasServer for state management.
    """
    if not await _auth_ws(websocket):
        await websocket.close(code=4001)
        return

    canvas_srv = _get_canvas_server()

    # Use the canvas server's handler via its internal pattern
    await websocket.accept()

    # Register as a canvas connection
    if session_id not in canvas_chat_manager:
        canvas_chat_manager[session_id] = []
    canvas_chat_manager[session_id].append(websocket)

    logger.info("[Server] Canvas WebSocket connected: %s", session_id)

    # Send initial state sync
    state = await canvas_srv.get_state(session_id)
    try:
        await websocket.send_text(json.dumps({
            "message_type": "sync",
            "session_id": session_id,
            "components": state.get("components", []),
        }))
    except Exception as exc:
        logger.error("[Server] Failed to send canvas sync: %s", exc)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "message_type": "error",
                    "error": "Invalid JSON",
                }))
                continue

            msg_type = msg.get("message_type", "")

            if msg_type == "ping":
                await websocket.send_text(json.dumps({"message_type": "pong"}))
                continue

            if msg_type == "sync":
                state = await canvas_srv.get_state(session_id)
                await websocket.send_text(json.dumps({
                    "message_type": "sync",
                    "session_id": session_id,
                    "components": state.get("components", []),
                }))
                continue

            if msg_type == "event":
                # Forward events to registered handlers
                from app.canvas.a2ui import event_from_dict
                event = event_from_dict(msg)
                logger.debug("[Server] Canvas event: %s -> %s", event.component_id, event.action)
                # Events could trigger agent actions via canvas event handlers
                continue

            # Handle update/clear from the server-side (agent → viewer is via canvas_srv.update)

    except WebSocketDisconnect:
        logger.info("[Server] Canvas WebSocket disconnected: %s", session_id)
    finally:
        conns = canvas_chat_manager.get(session_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            canvas_chat_manager.pop(session_id, None)


def serve() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="ManusClaw Agent Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "app.server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
