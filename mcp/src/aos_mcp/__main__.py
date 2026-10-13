"""Run only the authenticated loopback Streamable HTTP transport."""

from __future__ import annotations

import argparse
import os
from dataclasses import replace

import uvicorn

from aos_mcp.server import create_app
from aos_mcp.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="aOS authenticated MCP proxy")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        settings = Settings.from_env()
        if "AOS_MCP_ALLOWED_HOSTS" not in os.environ:
            settings = replace(
                settings,
                allowed_hosts=(
                    "127.0.0.1",
                    "localhost",
                    f"127.0.0.1:{args.port}",
                    f"localhost:{args.port}",
                ),
            )
    except ValueError:
        parser.error("invalid MCP configuration; check loopback URL and exact allowed hosts")
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=args.port,
        workers=1,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
