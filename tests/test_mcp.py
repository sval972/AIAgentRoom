import asyncio
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from agent_room.core import uid
from agent_room.server import TOOLS


def test_installed_stdio_from_other_directory(tmp_path):
    config = tmp_path / "room config.toml"
    example = Path(__file__).resolve().parents[1] / "examples" / "agent-room.example.toml"
    config.write_text(
        example.read_text()
        .replace("<your-existing-resource-host>", "example.invalid")
        .replace("context_tokens = 0", "context_tokens = 8192")
        .replace("max_output_tokens = 0", "max_output_tokens = 256")
    )

    def connect():
        return Client(
            StdioTransport(
                sys.executable,
                ["-m", "agent_room", "--config", str(config)],
                cwd=str(tmp_path),
                keep_alive=False,
            )
        )

    async def scenario():
        async with connect() as client:
            assert {t.name for t in await client.list_tools()} == set(TOOLS)
            models = await client.call_tool("list_models", {})
            assert models.data["models"][0]["alias"] == "reviewer"
            result = await client.call_tool(
                "create_room", {"request_id": uid(), "title": "test", "objective": "discuss"}
            )
            room = {k: result.data[k] for k in ("room_id", "room_proof")}
            await client.call_tool("send_message", dict(**room, request_id=uid(), body="An unaddressed note"))
        async with connect() as client:
            result = await client.call_tool("read_messages", room)
            assert result.data["messages"][0]["body"] == "An unaddressed note"

    asyncio.run(scenario())
