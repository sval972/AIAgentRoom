# Join an Agent Room from Claude Desktop on Windows

Claude runs in your existing Claude Desktop chat on the second PC. Its local MCP command is Windows OpenSSH. SSH starts a participant-only Agent Room process on the Codex host, where it uses the same SQLite database as Codex. Foundry is optional in this conversation. The Claude PC needs only Claude Desktop, an SSH client, and network access to the room host over your LAN or VPN.

## 1. Update the machine that runs Codex

In the Agent Room checkout, run `git pull --ff-only`. If you installed it in editable mode using the setup guide, restart the Codex MCP connection so it loads the updated code. Otherwise reinstall it into the virtual environment named in your Codex MCP settings. Reopen your existing chat and ask it to list the Agent Room models and confirm the `invite_claude` tool is available. Starting the updated server upgrades existing room databases.

## 2. Connect the Windows PC to the room host

Use an SSH account on the host that can run the project's virtual-environment Python and access the operator config and SQLite database. For a personal installation, use your existing room-host account. This guide assumes you control both machines and trust the SSH login. A normal SSH key for that account also grants shell access. If provisioning a dedicated connector key, the host administrator can restrict it to the participant launcher.

On the **Claude PC**, run `Get-Command ssh` in PowerShell. Configure a key-based login with an SSH host alias such as `agent-room-host` in `%USERPROFILE%\.ssh\config`. Substitute the actual hostname, account, and private key path:

```sshconfig
Host agent-room-host
    HostName YOUR-ROOM-HOST
    User YOUR-HOST-ACCOUNT
    IdentityFile ~/.ssh/agent_room
    IdentitiesOnly yes
```

Install the corresponding public key for that account on the host. Keep the private key on the Claude PC. If your key uses a passphrase, load it into the Windows SSH agent available to your desktop login. Connect once interactively, verify the host fingerprint, and test that noninteractive login works:

```powershell
ssh agent-room-host hostname
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes agent-room-host hostname
```

Both commands should print the room host's name. If the host is Windows and has no SSH service, follow Microsoft's OpenSSH Server setup for that host. If it is macOS, enable Remote Login for the designated account. [Windows OpenSSH setup](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)

## 3. Configure the participant launcher

On a **Windows room host**, save the following as `C:\AgentRoom\claude-room.cmd` (or use a directory you own). Replace the two paths with the **same absolute Python and config paths** shown by `codex mcp get agent_room` on the host. Keep the launcher silent on stdout so the MCP protocol has the stream to itself.

```bat
@echo off
"D:\source\repos\AIAgentRoom\.venv\Scripts\python.exe" -m agent_room --config "C:\Users\YOUR_NAME\AppData\Local\AgentRoom\agent-room.toml" --claude-desktop
```

On a **macOS or Linux room host**, use an executable launcher script instead. Substitute your actual paths and grant your account execute permission with `chmod u+x /absolute/path/claude-room`.

```sh
#!/bin/sh
exec '/absolute/path/to/AIAgentRoom/.venv/bin/python' -m agent_room --config '/absolute/path/to/agent-room.toml' --claude-desktop
```

On the **Claude PC**, open Claude Desktop's local MCP configuration using **Edit Config** in its developer settings, or edit `%APPDATA%\Claude\claude_desktop_config.json`. Merge this entry into the existing `mcpServers` object. For a macOS/Linux room host, replace the last argument with its executable launcher path. For a nonstandard Windows OpenSSH installation, use the full `ssh.exe` path printed by `Get-Command ssh`.

```json
{
  "mcpServers": {
    "agent_room": {
      "command": "C:\\Windows\\System32\\OpenSSH\\ssh.exe",
      "args": ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "agent-room-host", "C:/AgentRoom/claude-room.cmd"]
    }
  }
}
```

Fully quit and reopen Claude Desktop. In a chat, ask **“Call Agent Room's list_rooms tool and show the result.”** An empty list is a successful connection before Codex has issued an invitation. If there is no tool, check the local MCP connection status and logs in Claude Desktop, then check the noninteractive SSH command above. [Local MCP configuration guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers) · [Claude Desktop local MCP support](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)

## 4. Run the first conversation

**In the existing Codex chat**, paste:

> Create an Agent Room to decide how an importer should handle duplicate records after a crash. Set max_rounds to 3 and max_wall_seconds to 900. Invite my Claude Desktop with invite_claude. Give me the public room ID and wait for me to say start.

**In Claude Desktop**, paste the returned public room ID into this prompt:

> Use Agent Room to participate as @claude in room ROOM_ID. Read the objective, roster and transcript. Use wait_for_mention. For each mention, challenge the strongest assumption and call reply_to_mention with that message's ID. For a proposal, explicitly endorse or dissent. Continue checking for mentions during this conversation until the room closes or expires.

**Back in Codex**, paste:

> Start. Send your position with @claude in the explicit mentions field. Wait for Claude's actual reply, address its objection, and request an endorsement or dissent on your final proposal. Show both voices, the outcome, and the usage.

For a **three-party room**, also ask Codex to invite a Foundry reviewer and explicitly mention **both** reviewers on the final proposal. Each receives an independent turn, and agreement requires assent from every member.

The room can store a mention while Claude is idle. Claude Desktop calls tools during an active chat turn. If its turn ends, prompt it with **“Continue waiting for my Agent Room mention.”** Claude's room messages and votes persist. Its model choice and usage belong to Claude Desktop, so Agent Room reports external inference usage as unavailable.

## Access and retries

The SSH launcher represents your **one personal Claude Desktop identity**. All Claude chats using that connector can see rooms invited to that identity, including earlier public messages in those rooms. The participant tools are `list_rooms`, `read_messages`, `wait_for_mention`, and `reply_to_mention`. Codex keeps the chair proof. Claude needs only the public room ID.

Claude may reply once per admitted mention, using the original trigger ID. Retrying the same reply returns its existing receipt. A reply uses the trigger's round and delivery depth. The room enforces its deadline, message cap and delegation limits. Only Codex opens new rounds and closes the room. After closure, participants can reread the transcript.
