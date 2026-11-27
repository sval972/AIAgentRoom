# Agent Room setup and acceptance guide

For the Codex-guided, single-machine setup with local Claude Code, Foundry, or both, start with the [copyable repository prompt](README.md#give-codex-the-repository). The steps below cover manual Foundry configuration and acceptance testing.

Use this guide to install Agent Room, connect an existing Codex chat, and run the acceptance scenario on your machine. The room rules and tool contracts are in the [implementation specification](SPECIFICATION.md).

## Local setup

Use Python 3.12, Codex with local MCP access, and an Entra identity already authorized to run inference on the existing Foundry resource. For developer sign-in through Azure CLI, run `az login` under the same OS user that launches Codex and select the correct tenant when needed. The adapter uses `DefaultAzureCredential`. If access is denied, report the tenant, alias and required inference permission without exposing the token. No step requires creating an Azure resource. [Microsoft Entra authentication for Foundry](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/configure-entra-id)

**Windows, in PowerShell from the repository root**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

**macOS, in a terminal from the repository root**

```sh
python3.12 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest
```

After dependency installation, the tests run offline using local fixtures. Copy the [operator configuration example](examples/agent-room.example.toml) into your local application-data directory and edit it there. Keep that directory private to your OS user because the database contains transcripts and private room receipts. Codex launches `python -m agent_room --config <absolute-path>` over stdio. Relative database paths resolve against the configuration file.

### Operator registry example

Copy the example into a private `agent-room.toml`. Replace the placeholders with values from the deployment's existing working Chat Completions request. The endpoint setting below is the HTTPS **origin**. The adapter constructs the path. Choose `v1` for an existing `/openai/v1/chat/completions` request. Choose `dated` for an existing deployment-qualified request and set `api_version` to its current working value. Set the token scope for that specific existing resource. [Microsoft Chat Completions REST reference](https://learn.microsoft.com/en-us/azure/foundry/openai/latest)

```toml
[server]
client_profile = "personal-codex"
database = "agent-room.sqlite3"

[endpoints.personal]
provider = "azure_openai_chat"
origin = "https://<your-existing-resource-host>"
route = "v1"
token_scope = "https://cognitiveservices.azure.com/.default" # verify for your working endpoint
# api_version = "<existing-api-version>"  # required for route = "dated"

[models.reviewer]
enabled = true
endpoint = "personal"
deployment = "<your-existing-deployment-name>"
input_bound_policy = "deployment_context_ceiling"
context_tokens = 0     # replace with the deployment's verified positive limit
max_output_tokens = 0 # replace with a supported positive output cap
output_cap_field = "max_completion_tokens" # use the field supported by your deployment

[models.reviewer.profiles.standard]
description = "Standard discussion"
# Set only options supported by this deployment, for example reasoning_effort.
```

The zeros deliberately require replacement. The shipped parser must reject them with a field-specific diagnostic. The adapter accepts an allowlisted output parameter such as `max_completion_tokens` or `max_tokens` and sets it itself. On a deployment that supports it, a second profile such as `[models.reviewer.profiles.deep]` can declare `reasoning_effort = "high"`. Codex selects the profile ID. Register additional deployments under independent aliases. Codex discovers these aliases via `list_models` and can instantiate multiple identities from one alias.

For a deployment using `/openai/v1/responses`, define an endpoint with `provider = "azure_openai_responses"` and `route = "v1"`, then point its model alias at that endpoint and set `output_cap_field = "max_output_tokens"`. Agent Room sends the conversation as input with `store = false` and keeps the room transcript in SQLite. [Microsoft Responses REST reference](https://learn.microsoft.com/en-us/rest/api/aifoundry/azureopenai/responses)

For a USD budget, add a dated price card to the model entry. The following **fictional test rates** also define the offline accounting fixture. For live use, enter the rates and billing categories that apply to the authorized deployment. Preserve the card revision on each attempt. An unset rate is unknown. A conservative context ceiling can reserve considerably more than a short prompt uses, so use a verified tighter bound when a small budget requires it.

```toml
[models.reviewer.pricing]
revision = "mock-fixture-2026-01-01"
currency = "USD"
input_per_million = "1.00"
cached_input_per_million = "0.50"
output_per_million = "2.00"
```

### Codex connection

Merge one matching entry into the user's Codex `config.toml`. Replace the paths with absolute paths on that machine. Keep the project's virtual environment as the executable so a GUI launch works without activating a shell. The MCP client timeout below exceeds the server's maximum 20-second wait. [Codex MCP configuration](https://developers.openai.com/codex/mcp/)

**Windows example**

```toml
[mcp_servers.agent_room]
command = 'D:\source\repos\AIAgentRoom\.venv\Scripts\python.exe'
args = ['-m', 'agent_room', '--config', 'C:\Users\YOUR_NAME\AppData\Local\AgentRoom\agent-room.toml']
startup_timeout_sec = 20
tool_timeout_sec = 45
```

**macOS example**

```toml
[mcp_servers.agent_room]
command = '/Users/YOUR_NAME/src/AIAgentRoom/.venv/bin/python'
args = ['-m', 'agent_room', '--config', '/Users/YOUR_NAME/Library/Application Support/AgentRoom/agent-room.toml']
startup_timeout_sec = 20
tool_timeout_sec = 45
```

From a shell, use `codex mcp list` and `codex mcp get agent_room` to check registration. In the existing project chat, call `list_models` to verify the live connection. If the host has cached its tool list, reload its MCP configuration or restart the host and reopen the **same conversation**. Record the host version and behavior during live acceptance. Seeing a configuration entry alone does not pass the connection check. The project [AGENTS.md](AGENTS.md) supplies participation instructions. [Codex project instructions](https://developers.openai.com/codex/guides/agents-md/)

## Live acceptance from the existing chat

Use one of the operator's authorized deployments, enable a meaningful finite output cap and set token/cost budgets the verified bounds can admit. A single existing deployment suffices. Run the live scenario only when the operator requests it. It incurs inference usage.

> Start an Agent Room with a Foundry reviewer. In at most three rounds, decide how my importer should handle the same source record arriving twice, including after a crash. Start with a proposal, challenge each other's assumptions, then give me your shared recommendation or the exact point where you still disagree. Show me the exchange and its usage.

| Round | Example of observable behavior | Evidence to inspect |
| --- | --- | --- |
| 1 | Codex suggests checking for a source ID before writing. The reviewer identifies a failure window or a concurrent duplicate. | Distinct participants, one activating Codex send, one genuine Foundry reply with a parent ID. |
| 2 | Codex challenges a proposed fix using a crash or collision case. Foundry explains what must be atomic or revises its position. | The second reply engages with a prior message. Both agents contribute reasoning and the user sees an attributed update. |
| 3, if needed | Codex posts the exact recommendation as a proposal and explicitly addresses the reviewer. Foundry endorses it or records dissent in a direct reply. | Agreement references the proposal and independent endorsement. An unresolved disagreement records the issue and ends within the same limit. |

**Passing the primary demonstration** requires a live run with a substantive challenge from each side and an explicit shared recommendation within the requested limit. A clean disagreement is a passing test of the stop policy, but does not establish the agreement criterion. Exact wording is free. No participant may invent the other participant's assent. A successful earlier agreement may end the room before round three.

Verify that the existing chat shows the exchange and final recommendation, the transcript can be reread with its original IDs after restart, and a fourth activating send is rejected. Compare `get_usage` to the recorded attempts, including Codex's external metering status. Verify that closure remains readable after the final message and no further model work starts.

## Offline verification required before the live run

Use a deterministic mock adapter with scripted drafts, usage, delay and faults. Inject a clock and use barriers to control races. Exercise the core against real temporary SQLite files and exercise the boundary with a FastMCP client. Run at least one stdio subprocess test per supported OS. HTTP tests substitute a fake transport and credential so they never need Azure.

| Case | Expected evidence |
| --- | --- |
| Happy path and honest disagreement | Script three rounds with a genuine objection and revision. Check message order, parent IDs, mentions, evidence and outcome. Repeat with dissent, a revised proposal without re-endorsement, one missing vote in a three-member room, and an attempt to declare unilateral agreement. |
| Registry and identities | Two authorized aliases discoverable. An unknown or disabled alias and unsupported profile fail. Two identities using the same alias retain different instructions and effort profiles. Create and invite an agent into an already active room. Verify that creation and invitation consume zero inference. |
| Addressing and loop termination | An unaddressed post, quoted handle, reply link alone, and invitation never activate anyone. Repeated addresses coalesce. A chair-to-A-to-B handoff increments depth within one round. A-to-B-to-A terminates with a delivery disposition. A fresh chair turn resets depth. |
| Isolation and persistence | Interleave rooms containing the same managed agent. Restart the server between turns. Each prepared request contains its own earlier positions and the right trigger, with no private instructions or transcript from the other room. Exercise truncation and an oversized required parent. |
| Authority and untrusted content | Reject a forged sender, guessed room ID, wrong proof, cross-room parent/mention, endpoint override and unauthorized invitation. A transcript containing fabricated role markers, tool requests and registry entries remains a peer message in the captured provider payload. Confirm tokens, private room proofs, and private instructions are absent from public messages and logs. |
| Ordering, waits and idempotency | Parallel sends get distinct ordered sequences. Cursor pages concatenate without gaps or duplicates. Repeating the same request before and after restart returns the same receipt. Changed payload with the same key fails. Test reply-before-wait, wait-before-reply, unrelated author, poll timeout, close-during-wait and cross-process commit. |
| Concurrent execution and crashes | Two workers on one database never execute the same session concurrently. Independent sessions can both run. Interrupt before dispatch, after dispatch, and during final commit. Verify no duplicate charge reservation or publication, safe reclamation of pre-dispatch work, and quarantine of ambiguous dispatches even after lease expiry. |
| Limits and close races | Exercise each room and member limit, including exhausted final-round time, simultaneous fan-out near the message cap, and a late provider result after closure. Check that terminal deliveries are visible, the final round can finish, and reads and closure work with zero budget. |
| Accounting fixture | With fictional rates of USD 1/M uncached input, 0.50/M cached input, and 2/M output, report USD 0.0013 for 1,000 input tokens including 200 cached, plus 200 output tokens. Reasoning tokens included in those 200 output tokens add no second charge. Split assertions by member and room, include zero-message failures and external unknowns. |
| Competing budgets | With 1,000 tokens left and two concurrent 600-token reservations, at most one dispatches. Settling it at 400 releases 200 from its reservation, permitting the other request. Repeat for the room USD cap, individual member caps, unavailable pricing, missing usage and an uncertain billable attempt. |
| Foundry REST mapping | Assert exact route, deployment selection, bearer audience/refresh, supported reasoning and output-cap fields and redaction. Exercise expired credentials, 403, 429, transport loss, malformed JSON, empty or filtered replies and missing usage. Check that transport retries never duplicate inference. |
| Portability and contract | A mock provider with different option names and a fake external client exchange the same version-1 messages. Launch from an unrelated working directory on Windows and macOS, including a path with spaces. Obtain a real tool result through stdio on each OS. |

The delivery evidence is the offline test result on each OS and a live transcript with the room ID, proposal/endorsement IDs, rounds used, elapsed time, per-participant usage, and closure outcome. Keep access proofs, bearer tokens, private prompts, and machine-specific credentials out of that evidence.
