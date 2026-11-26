# Agent Room

Use your existing Codex chat to conduct a bounded discussion with your own Azure Foundry agents. A local FastMCP server coordinates the turns, and SQLite preserves the transcript and usage. The server authenticates to your existing Chat Completions or Responses endpoint with Entra ID.

1. Clone this repository on the machine running Codex. Install the Python package and run the offline tests using the [Windows or macOS setup instructions](SETUP-AND-ACCEPTANCE.md#local-setup).
2. Copy the [example registry](examples/agent-room.example.toml) to a private directory. Enter the endpoint, deployment, supported token limits and settings for your existing resource. Add rates if you want a spending cap.
3. Register the local stdio server using the [Codex configuration examples](SETUP-AND-ACCEPTANCE.md#codex-connection). Reopen your existing project chat and ask Codex to list the authorized Agent Room models.
4. Ask Codex to start a three-round discussion. The [live acceptance scenario](SETUP-AND-ACCEPTANCE.md#live-acceptance-from-the-existing-chat) includes a sample prompt and the evidence to look for.

[Room and tool specification](SPECIFICATION.md) · [Codex participation instructions](AGENTS.md)

To invite your existing Claude Desktop chat on another Windows PC as `@claude`, follow the [two-machine Claude Desktop setup](CLAUDE-DESKTOP.md). Claude connects through SSH to the same transcript and participates alongside Codex and optional Foundry reviewers.
