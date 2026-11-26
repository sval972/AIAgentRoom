import argparse
import os

from .server import configured_server


def main():
    if os.name == "posix":
        os.umask(0o077)
    parser = argparse.ArgumentParser(description="Agent Room local MCP server")
    parser.add_argument("--config", required=True, help="Path to the operator TOML registry")
    args = parser.parse_args()
    configured_server(args.config).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
