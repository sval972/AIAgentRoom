import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import setup_agent_room as setup


def test_setup_adds_both_sources_and_reruns_without_replacing_config(tmp_path, monkeypatch):
    root = tmp_path / "repo with spaces"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(setup, "claude_command", lambda _: ["claude"])
    monkeypatch.setattr(setup.shutil, "which", lambda name: str(tmp_path / name))
    calls = []
    registered = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["mcp", "get"]:
            if not registered:
                return subprocess.CompletedProcess(command, 1, "", "No MCP server named 'agent_room' found.")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {"transport": {"type": "stdio", "command": registered[0], "args": registered[1:]}}
                ),
                "",
            )
        if command[1:3] == ["mcp", "add"]:
            registered.extend(command[5:])
        if "--check" in command:
            assert {"claude", "foundry"} <= tomllib.loads(Path(command[-1]).read_text())["models"].keys()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    config = tmp_path / "private" / "existing.toml"
    config.parent.mkdir()
    original = '[server]\nclient_profile="me"\ndatabase="keep.sqlite3"\n# my private settings\n'
    config.write_text(original)
    flags = [
        "--claude",
        "--foundry-url",
        "https://existing.invalid/openai/v1/responses",
        "--deployment",
        "my-deployment",
        "--context-tokens",
        "32000",
    ]
    setup.install(setup.parser().parse_args(["--config", str(config), *flags]))
    first = config.read_bytes()
    setup.install(setup.parser().parse_args(flags))
    assert config.read_bytes() == first and config.read_text().startswith(original)
    assert len([c for c in calls if c[1:3] == ["mcp", "add"]]) == 1
    assert len([c for c in calls if "--check" in c]) == 2
    assert registered == [str(python), "-m", "agent_room", "--config", str(config)]
    assert tomllib.loads(config.read_text())["models"]["foundry"]["output_cap_field"] == "max_output_tokens"


def test_foundry_dated_and_conflicts():
    flags = [
        "--foundry-url",
        "https://host.invalid/openai/deployments/a%20b/chat/completions?api-version=test",
        "--context-tokens",
        "32000",
        "--output-cap-field",
        "max_tokens",
    ]
    args = setup.parser().parse_args(flags)
    text = setup.registry_text(None, args)
    config = tomllib.loads(text)["models"]["foundry"]
    assert (config["route"], config["api_version"], config["deployment"]) == ("dated", "test", "a b")
    with pytest.raises(ValueError, match="different settings"):
        setup.registry_text(text, setup.parser().parse_args([*flags, "--context-tokens", "8192"]))
    with pytest.raises(ValueError, match="differs"):
        setup.registry_text(None, setup.parser().parse_args([*flags, "--deployment", "another"]))
    with pytest.raises(ValueError, match="Select --claude"):
        setup.registry_text(None, setup.parser().parse_args([]))


def test_cli_only_discovery_check(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\ndatabase="local.sqlite3"\nclient_profile="local"\n[models.claude]\nprovider="claude_cli"\n'
    )
    result = subprocess.run(
        [sys.executable, "-m", "agent_room", "--config", str(config), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Authorized models: claude" in result.stdout
