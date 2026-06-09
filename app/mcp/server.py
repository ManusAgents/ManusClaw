from __future__ import annotations

"""
FastAPI-based MCP server that exposes local ManusClaw tools to external MCP clients.
"""

import os
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.tool.base import ToolCollection
from app.tool.bash import Bash
from app.tool.browser_use_tool import BrowserUseTool
from app.tool.str_replace_editor import StrReplaceEditor
from app.tool.terminate import Terminate

# ---------------------------------------------------------------------------
# API Key authentication — enabled only when MANUSCLAW_API_KEY is set
# ---------------------------------------------------------------------------

_API_KEY = os.getenv("MANUSCLAW_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: Optional[str] = Depends(_api_key_header)) -> None:
    if not _API_KEY:
        return
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def build_mcp_server() -> FastAPI:
    app = FastAPI(title="ManusClaw MCP Server", version="1.0.0")

    # -------------------------------------------------------------------------
    # CORS — configurable via env; never mixes wildcard with credentials
    # -------------------------------------------------------------------------
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
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _tools = ToolCollection(
        Bash(),
        BrowserUseTool(),
        StrReplaceEditor(),
        Terminate(),
    )

    class CallRequest(BaseModel):
        name: str
        arguments: dict[str, Any] = {}

    @app.get("/tools/list", dependencies=[Depends(require_api_key)])
    async def list_tools() -> dict:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.parameters,
                }
                for t in _tools
            ]
        }

    @app.post("/tools/call", dependencies=[Depends(require_api_key)])
    async def call_tool(req: CallRequest) -> dict:
        tool = _tools.get(req.name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"Tool '{req.name}' not found.")
        result = await tool.execute(**req.arguments)
        return {"content": [{"type": "text", "text": str(result)}]}

    @app.get("/healthz")
    async def health() -> dict:
        return {"status": "ok", "tools": len(_tools)}

    return app
