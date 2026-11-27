"""Install this checkout and configure its local Codex MCP server (Python 3.12+)."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from agent_room.claude_cli import claude_command

ROOT = Path(__file__).resolve().parent


def default_config():
    if os.name == "nt":
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "AgentRoom/agent-room.toml"


def foundry_values(args):
    url = urlsplit(args.foundry_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Supply your existing HTTPS Foundry inference URL without credentials.")
    path = url.path.rstrip("/")
    query = parse_qs(url.query, keep_blank_values=True)
    if set(query) - {"api-version"}:
        raise ValueError("Only the api-version query parameter is supported.")
    dated = re.fullmatch(r"/openai/deployments/([^/]+)/chat/completions", path)
    responses = path == "/openai/v1/responses"
    fields = {
        "provider": "azure_openai_responses" if responses else "azure_openai_chat",
        "origin": f"https://{url.netloc}",
        "route": "dated" if dated else "v1",
        "token_scope": args.token_scope,
        "input_bound_policy": "deployment_context_ceiling",
    }
    if dated:
        versions = query.get("api-version", [])
        if len(versions) != 1 or not versions[0]:
            raise ValueError("The deployment-qualified URL needs its working api-version.")
        fields["api_version"] = versions[0]
        deployment = unquote(dated[1])
        if args.deployment and args.deployment != deployment:
            raise ValueError("The deployment in the URL differs from --deployment.")
    elif responses or path == "/openai/v1/chat/completions":
        if query and query.get("api-version") != ["v1"]:
            raise ValueError("This setup supports the stable v1 route or a dated deployment-qualified URL.")
        deployment = args.deployment
    else:
        raise ValueError("Copy the complete Chat Completions or Responses inference URL.")
    if not deployment or not args.context_tokens or not 0 < args.output_tokens < args.context_tokens:
        raise ValueError("Supply the deployment name and verified context limit larger than the output cap.")
    field = args.output_cap_field or ("max_output_tokens" if responses else "max_completion_tokens")
    if field not in (("max_output_tokens",) if responses else ("max_tokens", "max_completion_tokens")):
        raise ValueError("Output cap field is incompatible with the selected Foundry route.")
    fields.update(
        deployment=deployment,
        context_tokens=args.context_tokens,
        max_output_tokens=args.output_tokens,
        output_cap_field=field,
    )
    return fields


def registry_text(original, args):
    text = (
        original
        if original is not None
        else '[server]\nclient_profile = "personal-codex"\ndatabase = "agent-room.sqlite3"\n'
    )
    cfg = tomllib.loads(text)
    if not all(cfg.get("server", {}).get(k) for k in ("database", "client_profile")):
        raise ValueError("Existing registry needs server.database and server.client_profile.")
    models = cfg.get("models", {})
    requested = {}
    if args.claude:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.claude_alias):
            raise ValueError("Choose a Claude alias using letters, digits, underscores or hyphens.")
        current = models.get(args.claude_alias, {})
        launcher = args.claude_executable or current.get("executable", "claude")
        claude_command(launcher)
        fields = {"provider": "claude_cli"}
        if args.claude_model:
            fields["deployment"] = args.claude_model
        # Capture the installed launcher path, so Codex can launch it from a different directory.
        if args.claude_executable or not current:
            fields["executable"] = str(Path(shutil.which(launcher)).absolute())
        requested[args.claude_alias] = fields
    if args.foundry_url:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.alias) or args.alias in requested:
            raise ValueError("Choose a distinct Foundry alias using letters, digits, underscores or hyphens.")
        requested[args.alias] = foundry_values(args)
    if not requested and not any(m.get("enabled", True) for m in models.values()):
        raise ValueError("Select --claude, supply --foundry-url, or select an existing configured registry.")
    for alias, fields in requested.items():
        if alias in models:
            prior = models[alias]
            effective = prior | cfg.get("endpoints", {}).get(prior.get("endpoint"), {})
            if not prior.get("enabled", True) or any(effective.get(k) != v for k, v in fields.items()):
                raise ValueError(
                    f"Alias {alias!r} already exists with different settings. Review the existing registry."
                )
            continue
        text += f"\n[models.{alias}]\n" + "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields.items()
        )
        text += f'\n[models.{alias}.profiles.standard]\ndescription = "Standard discussion"\n'
    tomllib.loads(text)
    return text


def registration(codex):
    result = subprocess.run(
        [codex, "mcp", "get", "--json", "agent_room"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        if "No MCP server named" in result.stderr:
            return None
        raise ValueError("Could not inspect Codex MCP registration. Check codex mcp list.")
    data = json.loads(result.stdout)
    transport = data.get("transport", {})
    args = transport.get("args", [])
    if transport.get("type") != "stdio" or args[:3] != ["-m", "agent_room", "--config"] or len(args) != 4:
        raise ValueError("agent_room is registered to a different command. Review codex mcp get agent_room.")
    if not data.get("enabled", True):
        raise ValueError("Enable the existing agent_room MCP registration in Codex settings first.")
    return Path(transport["command"]), Path(args[3])


def install(args):
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("Install the Codex CLI or make it available on PATH for MCP registration.")
    existing = registration(codex)
    config = (args.config or (existing[1] if existing else default_config())).expanduser().absolute()
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if existing and (existing[0].absolute() != python or existing[1].absolute() != config):
        raise ValueError(
            "Existing agent_room registration points elsewhere. Run setup in its checkout or review it in Codex."
        )
    original = config.read_text(encoding="utf-8") if config.exists() else None
    updated = registry_text(original, args)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    subprocess.run([str(python), "-m", "pip", "install", "-e", str(ROOT)], check=True)
    if original != updated:
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config.parent, delete=False) as out:
            staging = Path(out.name)
            try:
                out.write(updated)
            except BaseException:
                out.close()
                staging.unlink(missing_ok=True)
                raise
        try:
            subprocess.run([str(python), "-m", "agent_room", "--check", "--config", str(staging)], check=True)
            if (config.read_text(encoding="utf-8") if config.exists() else None) != original:
                raise ValueError(
                    "The registry changed during setup. Rerun to merge the current configuration."
                )
            os.replace(staging, config)
        finally:
            staging.unlink(missing_ok=True)
    else:
        subprocess.run([str(python), "-m", "agent_room", "--check", "--config", str(config)], check=True)
    if not existing:
        subprocess.run(
            [
                codex,
                "mcp",
                "add",
                "agent_room",
                "--",
                str(python),
                "-m",
                "agent_room",
                "--config",
                str(config),
            ],
            check=True,
        )
    print(f"Agent Room is registered. Registry: {config}\nIn your Codex chat, call agent_room.list_models.")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--config",
        type=Path,
        help="Registry path (default: existing registration or user app-data directory)",
    )
    p.add_argument("--claude", action="store_true", help="Enable the signed-in local Claude Code CLI")
    p.add_argument("--claude-alias", default="claude", help="Claude registry alias")
    p.add_argument("--claude-model", help="Operator-selected model (defaults to the CLI's selected model)")
    p.add_argument("--claude-executable", help="Path to a custom Claude CLI installation")
    p.add_argument("--foundry-url", help="Complete HTTPS Chat Completions or Responses URL")
    p.add_argument("--alias", default="foundry", help="Foundry registry alias")
    p.add_argument("--deployment", help="Deployment name for a Foundry v1 URL")
    p.add_argument("--context-tokens", type=int, help="Verified Foundry deployment context limit")
    p.add_argument("--output-tokens", type=int, default=1024)
    p.add_argument("--output-cap-field", choices=["max_tokens", "max_completion_tokens", "max_output_tokens"])
    p.add_argument("--token-scope", default="https://cognitiveservices.azure.com/.default")
    return p


if __name__ == "__main__":
    try:
        install(parser().parse_args())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
