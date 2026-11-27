import argparse
import asyncio
import os
import sys
from pathlib import Path

from .server import configured_server


async def check(config):
    """Check the same installed stdio entry point that Codex will launch, without inference."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        sys.executable, ["-m", "agent_room", "--config", str(Path(config).resolve())], keep_alive=False
    )
    async with Client(transport) as client:
        models = (await client.call_tool("list_models", {})).data["models"]
        print(
            "MCP discovery succeeded. Authorized models:", ", ".join(m["alias"] for m in models) or "(none)"
        )


def main():
    if os.name == "posix":
        os.umask(0o077)
    parser = argparse.ArgumentParser(description="Agent Room local MCP server")
    parser.add_argument("--config", required=True, help="Path to the operator TOML registry")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--claude-desktop", action="store_true", help="Serve invited Claude participant tools over stdio"
    )
    modes.add_argument(
        "--check", action="store_true", help="Check MCP registration targets without inference"
    )
    args = parser.parse_args()
    if args.check:
        asyncio.run(check(args.config))
        return
    configured_server(args.config, claude=args.claude_desktop).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
