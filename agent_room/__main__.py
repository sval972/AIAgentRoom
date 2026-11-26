import argparse
import os

from .server import configured_server


def main():
    if os.name == "posix":
        os.umask(0o077)
    parser = argparse.ArgumentParser(description="Agent Room local MCP server")
    parser.add_argument("--config", required=True, help="Path to the operator TOML registry")
    parser.add_argument(
        "--claude-desktop", action="store_true", help="Serve invited Claude participant tools over stdio"
    )
    args = parser.parse_args()
    configured_server(args.config, claude=args.claude_desktop).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
