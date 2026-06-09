#!/usr/bin/env python3
"""
ManusClaw WebSocket / REST Server

Usage:
    python run_server.py [--host HOST] [--port PORT] [--reload]

Then connect from manusclaw-web or any HTTP/WS client:
    http://localhost:8765/healthz
    ws://localhost:8765/ws/<session_id>
"""

import argparse
import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="ManusClaw Agent Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload (dev mode)")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install uvicorn[standard]")
        sys.exit(1)

    print(f"\n  ███╗   ███╗ █████╗ ███╗   ██╗██╗   ██╗███████╗")
    print(f"  ████╗ ████║██╔══██╗████╗  ██║██║   ██║██╔════╝")
    print(f"  ██╔████╔██║███████║██╔██╗ ██║██║   ██║███████╗")
    print(f"  ██║╚██╔╝██║██╔══██║██║╚██╗██║██║   ██║╚════██║")
    print(f"  ██║ ╚═╝ ██║██║  ██║██║ ╚████║╚██████╔╝███████║")
    print(f"  ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚══════╝")
    print(f"  ManusClaw v4.0.0 — Agent Server  by The-JDdev (SHS Shobuj)")  # Fix: sync version
    print(f"\n  Listening: http://{args.host}:{args.port}")
    print(f"  REST:      http://{args.host}:{args.port}/run")
    print(f"  WebSocket: ws://{args.host}:{args.port}/ws/<session_id>")  # Fix: ws:// not wss:// (no TLS)
    print(f"  Sessions:  http://{args.host}:{args.port}/sessions")
    print(f"  Health:    http://{args.host}:{args.port}/healthz\n")

    uvicorn.run(
        "app.server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
