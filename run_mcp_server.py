#!/usr/bin/env python3
"""
ManusClaw — MCP Server entry point.
Hosts a FastAPI MCP server that exposes local tools to external clients.

Usage:
    python run_mcp_server.py [--host 0.0.0.0] [--port 8000]
"""
import argparse

import uvicorn

from app.mcp.server import build_mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(description="ManusClaw MCP Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = build_mcp_server()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
