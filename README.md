# Agent Room

Use one Codex chat to run a bounded conversation among locally signed-in Claude Code agents and models on your existing Foundry resource. One local MCP server stores the transcript in SQLite. Each invited participant has its own identity, role and room history.

## Give Codex the repository

Paste this into the Codex chat on the machine that will run the room:

> Set up Agent Room from https://github.com/sval972/AIAgentRoom on this machine. Clone the repo or reuse my checkout, read AGENTS.md, and run its setup. Ask me whether to enable local Claude Code, Foundry, or both. Reuse my existing configuration. Walk me through any sign-in locally. Verify the MCP connection, show me the available agent sources, and ask which agents and how many of each I want in my first room.

After setup, try:

> Start a three-round room about how to handle duplicate imports after a crash. Invite two Claude Code agents, one testing failure cases and one reviewing operational complexity, plus one Foundry reviewer. Have each of them challenge the proposal. Ask them to endorse or dissent on the final recommendation, and show me the exchange.

Codex is the chair, so this example has four participants. You can also request a Claude-only or Foundry-only room. Agent creation and discovery spend no inference. Mentioning the selected agents starts their turns.

## What setup does

Run from the checkout with Python 3.12+ and the Codex CLI available. On Windows, use `py -3.12` in place of `python3.12`. The setup command creates or reuses `.venv`, installs the package, merges the selected providers into your private TOML registry, exercises the real stdio server with `list_models`, and registers `agent_room` with Codex. It preserves compatible existing registrations and their database paths. Reopen the same Codex chat if its tools have not refreshed.

**Local Claude Code.** Install the official Claude Code CLI on the Codex machine. Run `claude` as the same OS user and sign in through Claude Code with your eligible Claude plan. Check the active account and billing method in the CLI if you have previously configured other credentials. Then run:

```sh
python3.12 setup_agent_room.py --claude
```

Agent Room invokes the user's installed `claude -p` once per addressed turn. Each invocation receives the agent's role and the relevant room transcript and runs with tools disabled. The CLI handles authentication. A Claude Desktop window, SSH connection, and Anthropic API key are unnecessary for the signed-in plan workflow. Plan limits still apply. Use `--claude-alias` and `--claude-model` to register additional model choices, or `--claude-executable` for a nonstandard installation path. [Claude Code programmatic usage](https://code.claude.com/docs/en/headless) · [Claude plan usage for print mode](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)

**Foundry.** Use an existing registry, or sign in with your authorized Entra identity (for example, `az login`) and supply your existing full Chat Completions or Responses URL. For a v1 URL, also supply the deployment name. Enter the deployment's verified context limit and a supported output cap. Replace the placeholders below. Omit `--claude` for Foundry only.

```sh
python3.12 setup_agent_room.py --claude --foundry-url "https://YOUR-HOST/openai/v1/chat/completions" --deployment "YOUR-DEPLOYMENT" --context-tokens YOUR_CONTEXT_LIMIT --output-tokens 1024
```

For a deployment-qualified Chat Completions URL, paste the entire URL including its `api-version`. For Responses, use the existing `/openai/v1/responses` URL. Use `--output-cap-field max_tokens` if that is the field your Chat Completions deployment supports. Add other deployments by rerunning with a distinct `--alias`. Run with `--config PATH` for a custom registry. With an existing configured registry you can simply run the setup command without provider flags to install, verify and register it.

## Participants, limits and extensions

- Ask Codex to call `list_models` to discover authorized sources and `invite_agents` with one seat per requested participant. Creating a roster is atomic and retryable. Existing individual creation and invitation tools remain available.
- The room enforces round, time, delegation, turn and message limits for both sources. Foundry can also enforce reserved token and cost budgets. Claude Code reports its own usage after a turn, and its USD figure is an estimate under the CLI's accounting. A hard token or USD cap covering a CLI turn causes the server to decline that turn before launching the CLI. For mixed rooms, you can apply inference budgets specifically to the Foundry seats.
- To add a source, implement the provider-neutral adapter in `agent_room/contracts.py` and register its provider name and factory in `agent_room/server.py`. The adapter owns configuration defaults, validation, request preparation, execution, cleanup, and usage reporting. Room messages and roster tools are shared.
- For manual installation, registry examples, price cards and the live acceptance scenario, see the [Windows/macOS setup guide](SETUP-AND-ACCEPTANCE.md). The optional [Claude Desktop participant](CLAUDE-DESKTOP.md) supports a separate interactive desktop chat.

[Room specification](SPECIFICATION.md) · [Codex participation instructions](AGENTS.md)
