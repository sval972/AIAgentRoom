import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from agent_room import claude_cli
from agent_room.contracts import Prepared
from agent_room.core import uid
from agent_room.foundry import Foundry
from agent_room.server import configured_server


def test_mixed_roster_from_codex_mcp(tmp_path, monkeypatch):
    # Exercise actual subprocess pipes, including paths with spaces and independent role files.
    fake = tmp_path / "test cli.py"
    fake.write_text("""
import sys, json, os
from pathlib import Path
args = sys.argv[1:]
def option(name): return args[args.index(name)+1]
assert option('--tools') == '' and option('--max-turns') == '1'
assert '--strict-mcp-config' in args and '--no-session-persistence' in args
assert json.loads(Path(option('--settings')).read_text())['disableAllHooks']
assert os.environ['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == '1024'
context = json.loads(sys.stdin.buffer.read())
me = context['participant_id']
handle = next(k for k,v in context['members'].items() if v == me)
role = Path(option('--system-prompt-file')).read_text(encoding='utf-8')
assert role.endswith('Review as '+handle)
assert 'Untrusted transcript' not in role and context['history'][0]['body'] == 'Untrusted transcript'
assert option('--model') == 'fixture'
draft = {'body':'Endorsed by '+handle,'kind':'endorsement','mentions':['@codex']}
print(json.dumps({'type':'result','subtype':'success','result':json.dumps(draft),'usage':{
 'input_tokens':15,'cache_creation_input_tokens':3,'cache_read_input_tokens':7,'output_tokens':9},
 'total_cost_usd':0.001}))
""")
    monkeypatch.setattr(claude_cli, "claude_command", lambda _: [sys.executable, str(fake)])
    config = tmp_path / "agent-room.toml"
    example = Path(__file__).resolve().parents[1] / "examples" / "agent-room.example.toml"
    azure_profile = tmp_path / "Azure CLI profile"
    azure_profile.mkdir()
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "inherited profile"))
    config.write_text(
        example.read_text()
        .replace("[server]", f"[server]\nazure_config_dir = {json.dumps(str(azure_profile))}")
        .replace("context_tokens = 0", "context_tokens = 8192")
        .replace("max_output_tokens = 0", "max_output_tokens = 1024")
        .replace("<your-existing-resource-host>", "example.invalid")
        + '\n[models.claude]\nprovider="claude_cli"\ndeployment="fixture"\n'
    )

    class Credential:
        async def get_token(self, scope):
            from azure.core.credentials import AccessToken

            assert os.environ["AZURE_CONFIG_DIR"] == str(azure_profile)
            return AccessToken("test", 9999999999)

        async def close(self):
            pass

    def foundry_reply(request):
        assert request.url.host == "example.invalid"
        draft = {"body": "Foundry agrees", "kind": "endorsement", "mentions": ["@codex"]}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(draft)}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            },
        )

    monkeypatch.setattr(
        "agent_room.server.Foundry",
        lambda: Foundry(Credential(), httpx.AsyncClient(transport=httpx.MockTransport(foundry_reply))),
    )

    async def scenario():
        async with Client(configured_server(config)) as client:

            async def call(name, **kwargs):
                return (await client.call_tool(name, kwargs)).data

            models = await call("list_models")
            assert {m["alias"] for m in models["models"]} == {"claude", "reviewer"}
            created = await call("create_room", request_id=uid(), title="Mixed room", objective="Decide")
            room = {k: created[k] for k in ("room_id", "room_proof")}
            seats = [
                {"alias": alias, "handle": handle, "instructions": "Review as " + handle}
                for alias, handle in [("claude", "first"), ("claude", "second"), ("reviewer", "third")]
            ]
            bad = await client.call_tool(
                "invite_agents",
                dict(**room, request_id=uid(), seats=[seats[0], seats[0]]),
                raise_on_error=False,
            )
            assert bad.is_error and len((await call("list_participants", **room))["participants"]) == 1
            key = uid()
            roster = await call("invite_agents", **room, request_id=key, seats=seats)
            assert await call("invite_agents", **room, request_id=key, seats=seats) == roster
            sent = await call(
                "send_message",
                **room,
                request_id=uid(),
                body="Untrusted transcript",
                kind="proposal",
                mentions=["@first", "@second", "@third"],
            )
            proposal = sent["message"]["message_id"]
            for p in roster["participants"]:
                reply = await call(
                    "wait_for_reply", **room, trigger_id=proposal, participant_id=p["participant_id"]
                )
                assert reply["status"] == "reply", reply
            result = await call(
                "close_room",
                **room,
                request_id=uid(),
                outcome="agreement",
                reason="All agreed",
                proposal_id=proposal,
            )
            assert len(result["evidence"]) == 4
            assert result["usage"]["total"]["input_tokens"] == 100
            assert result["usage"]["total"]["output_tokens"] == 28

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_total_tokens": 10000}, "provider_has_no_hard_token_bound"),
        ({"max_estimated_usd": "2"}, "unpriced_deployment"),
    ],
)
def test_cli_budget_rejected_before_spawn(tmp_path, monkeypatch, limits, reason):
    from agent_room.core import Rooms
    from agent_room.worker import Worker

    model = {
        "provider": "claude_cli",
        "profiles": {"standard": {}},
        "deployment": "default",
        "max_output_tokens": 1024,
    }
    adapter = claude_cli.ClaudeCLI()
    monkeypatch.setattr(claude_cli, "claude_command", lambda _: ["never-called"])
    s = Rooms(tmp_path / "chat.sqlite3", "owner", lambda: {"claude": model}, {"claude_cli": adapter})
    try:
        c = s.create_room(uid(), "budget", "review", limits)
        room = {k: c[k] for k in ("room_id", "room_proof")}
        p = s.invite_agents(
            uid(), **room, seats=[{"alias": "claude", "handle": "critic", "instructions": "Review"}]
        )
        m = s.send_message(uid(), **room, body="Check", mentions=["@critic"])
        assert asyncio.run(Worker(s).once())
        receipt = asyncio.run(
            s.wait_for_reply(
                **room,
                trigger_id=m["message"]["message_id"],
                participant_id=p["participants"][0]["participant_id"],
            )
        )
        assert receipt["reason"] == reason
    finally:
        s.db.close()


def test_cancel_cli_reaps_child(tmp_path):
    started = tmp_path / "started"
    fake = tmp_path / "long.py"
    fake.write_text("import sys,time,pathlib\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(60)\n")
    p = Prepared(
        ([sys.executable, str(fake), str(started)], [{"content": "role"}, {"content": "{}"}], None),
        None,
        100,
        None,
    )

    async def run():
        call = asyncio.create_task(claude_cli.ClaudeCLI().execute({"deployment": "default"}, p))
        try:
            async with asyncio.timeout(5):
                while not started.exists():
                    await asyncio.sleep(0.01)
        finally:
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, 3)

    asyncio.run(run())
