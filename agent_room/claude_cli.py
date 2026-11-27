"""Run one text-only turn through the operator's signed-in Claude Code installation."""

import asyncio
import json
import os
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar

from .contracts import Prepared, Result, RoomError
from .core import require
from .discussion import discussion_messages

CLAUDE_PROVIDER = "claude_cli"


def claude_command(executable="claude"):
    """Use a native executable or the official npm entry point, never a command shell."""
    binary = shutil.which(executable)
    if not binary:
        raise RoomError("claude_cli_missing: install Claude Code on the MCP host or set its executable path")
    path = Path(binary).resolve()
    if os.name != "nt" or path.suffix.lower() == ".exe":
        return [str(path)]
    # An npm Windows installation exposes .cmd/.ps1 wrappers. Launch its package binary directly.
    package = path.parent / "node_modules" / "@anthropic-ai" / "claude-code"
    try:
        entry = package / json.loads((package / "package.json").read_text(encoding="utf-8"))["bin"]["claude"]
        if entry.is_file() and entry.suffix.lower() == ".exe":
            return [str(entry)]
        node = shutil.which("node.exe")
        if node and entry.is_file() and entry.suffix.lower() in (".js", ".cjs", ".mjs"):
            return [node, str(entry)]
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise RoomError("claude_cli_launcher_unsupported: select the installed claude.exe")


def parse_result(output):
    try:
        envelope = json.loads(output)
    except (UnicodeError, ValueError):
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        return Result(None, None, None, "claude_cli_invalid_output")
    raw = envelope.get("usage")
    usage = None
    if isinstance(raw, dict):
        counts = [
            raw.get("input_tokens"),
            raw.get("output_tokens"),
            raw.get("cache_read_input_tokens", 0),
            raw.get("cache_creation_input_tokens", 0),
        ]
        if all(type(x) is int and x >= 0 for x in counts):
            fresh, generated, cached, written = counts
            usage = {
                "input_tokens": fresh + cached + written,
                "output_tokens": generated,
                "cached_input_tokens": cached,
            }
    try:
        cost = Decimal(str(envelope["total_cost_usd"]))
        if not cost.is_finite() or cost < 0:
            cost = None
    except (KeyError, InvalidOperation):
        cost = None
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        return Result(None, usage, cost, "claude_cli_failed: check CLI sign-in, model access and plan limits")
    try:
        body = envelope["result"].strip()
        if body.startswith("```json\n") and body.endswith("\n```"):
            body = body[8:-4]
        draft = json.loads(body)
    except (KeyError, ValueError, AttributeError):
        return Result(None, usage, cost, "invalid_draft")
    return Result(draft, usage, cost)


class ClaudeCLI:
    registry_defaults: ClassVar[dict] = {
        "deployment": "default",
        "max_output_tokens": 4096,
        "profiles": {"standard": {"description": "Local Claude Code"}},
    }

    async def close(self):
        # Each turn reaps its own child before returning.
        pass

    def validate(self, config, profile):
        require(isinstance(config["deployment"], str) and config["deployment"].strip(), "invalid_cli_model")
        require(
            type(config["max_output_tokens"]) is int and 0 < config["max_output_tokens"] <= 131072,
            "invalid_max_output_tokens",
        )
        options = config["profiles"][profile]
        require(set(options) <= {"description", "effort"}, "unsupported_cli_profile_setting")
        if "effort" in options:
            require(options["effort"] in ("low", "medium", "high", "xhigh", "max"), "invalid_cli_effort")
        require(isinstance(config.get("executable", "claude"), str), "invalid_cli_executable")

    def prepare(self, config, profile, instructions, context, output_limit):
        command = claude_command(config.get("executable", "claude"))
        messages, truncated = discussion_messages(instructions, context, "system", 512_000)
        payload = (command, messages, config["profiles"][profile].get("effort"))
        return Prepared(payload, None, output_limit, None, truncated)

    async def execute(self, config, prepared):
        command, messages, effort = prepared.payload
        with tempfile.TemporaryDirectory(prefix="agent-room-") as work:
            instructions = Path(work) / "role.txt"
            instructions.write_text(messages[0]["content"], encoding="utf-8")
            settings = Path(work) / "settings.json"
            settings.write_text('{"disableAllHooks":true}', encoding="utf-8")
            args = [
                *command,
                "-p",
                "--output-format",
                "json",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--settings",
                str(settings),
                "--disable-slash-commands",
                "--no-session-persistence",
                "--max-turns",
                "1",
                "--system-prompt-file",
                str(instructions),
            ]
            if config["deployment"] != "default":
                args.extend(["--model", config["deployment"]])
            if effort:
                args.extend(["--effort", effort])
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=work,
                    env=os.environ | {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(prepared.output_bound)},
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                return Result(
                    None, {"input_tokens": 0, "output_tokens": 0}, Decimal(0), "claude_cli_launch_failed"
                )
            try:
                output, _ = await process.communicate(messages[1]["content"].encode("utf-8"))
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            result = parse_result(output)
            if process.returncode and (result.error is None or result.error == "claude_cli_invalid_output"):
                return Result(
                    None, result.usage, result.cost, "claude_cli_failed: check CLI version and sign-in"
                )
            return result
