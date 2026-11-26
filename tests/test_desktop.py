import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from agent_room.contracts import Prepared, Result, RoomError
from agent_room.core import Rooms, digest, uid
from agent_room.desktop import DesktopParticipant
from agent_room.worker import Worker


def room_args(result):
    return {k: result[k] for k in ("room_id", "room_proof")}


def test_two_stdio_clients(tmp_path):
    config = tmp_path / "host config.toml"
    config.write_text('[server]\ndatabase = "chat.sqlite3"\nclient_profile = "owner"\n')

    def connect(*extra):
        return Client(
            StdioTransport(
                sys.executable,
                ["-m", "agent_room", "--config", str(config), *extra],
                cwd=str(tmp_path),
                keep_alive=False,
            )
        )

    async def scenario():
        async with connect() as chair, connect("--claude-desktop") as guest:
            tools = {tool.name for tool in await guest.list_tools()}
            assert tools == {"list_rooms", "read_messages", "wait_for_mention", "reply_to_mention"}
            room = room_args(
                (
                    await chair.call_tool(
                        "create_room",
                        {"request_id": uid(), "title": "Demo", "objective": "Review a proposal"},
                    )
                ).data
            )
            rid = room["room_id"]
            assert (await guest.call_tool("list_rooms", {})).data["rooms"] == []
            assert (await guest.call_tool("read_messages", {"room_id": rid}, raise_on_error=False)).is_error
            invite = (await chair.call_tool("invite_claude", dict(**room, request_id=uid()))).data
            assert (await chair.call_tool("invite_claude", dict(**room, request_id=uid()))).data == invite
            roster = (await guest.call_tool("read_messages", {"room_id": rid})).data["participants"]
            assert [(p["handle"], p["provider"]) for p in roster] == [
                ("codex", "codex_host"),
                ("claude", "claude_desktop"),
            ]
            waiting = asyncio.create_task(guest.call_tool("wait_for_mention", {"room_id": rid, "timeout": 5}))
            sent = (
                await chair.call_tool(
                    "send_message",
                    dict(
                        **room, request_id=uid(), body="Atomic import", kind="proposal", mentions=["@claude"]
                    ),
                )
            ).data
            trigger = sent["message"]["message_id"]
            assert sent["deliveries"][0]["status"] == "notified"
            assert (await waiting).data["message"]["message_id"] == trigger
            reply_args = {"room_id": rid, "trigger_id": trigger, "kind": "endorsement", "body": "I agree."}
            reply = (await guest.call_tool("reply_to_mention", reply_args)).data
            assert reply["message"]["sender_id"] == invite["participant_id"]
            assert reply["message"]["round_no"] == 1 and reply["message"]["depth"] == 1
            receipt = await chair.call_tool(
                "wait_for_reply",
                dict(**room, trigger_id=trigger, participant_id=invite["participant_id"], timeout=0),
            )
            assert receipt.data["message"] == reply["message"]
            assert (await guest.call_tool("wait_for_mention", {"room_id": rid, "timeout": 0})).data[
                "status"
            ] == "timeout"
            closure = (
                await chair.call_tool(
                    "close_room",
                    dict(
                        **room,
                        request_id=uid(),
                        outcome="agreement",
                        proposal_id=trigger,
                        reason="Both agreed",
                    ),
                )
            ).data
            assert closure["evidence"] == [trigger, reply["message"]["message_id"]]
            usage = next(
                p for p in closure["usage"]["participants"] if p["participant_id"] == invite["participant_id"]
            )
            assert usage["message_count"] == 1 and usage["input_tokens"] is None
        async with connect("--claude-desktop") as guest:
            assert (await guest.call_tool("reply_to_mention", reply_args)).data == reply
            assert (
                await guest.call_tool(
                    "reply_to_mention", reply_args | {"body": "Changed"}, raise_on_error=False
                )
            ).is_error
            assert (await guest.call_tool("wait_for_mention", {"room_id": rid})).data["status"] == "closed"

    asyncio.run(scenario())


class Reviewer:
    def validate(self, config, profile):
        pass

    def prepare(self, config, profile, instructions, context, output_limit):
        return Prepared(context, 100, 100, None)

    async def execute(self, config, prepared):
        return Result(
            {"kind": "endorsement", "body": "Reviewed and agreed.", "mentions": ["@codex"]},
            {"input_tokens": 25, "output_tokens": 10},
            None,
        )


def test_three_member_agreement_and_isolation(tmp_path):
    db = tmp_path / "test.sqlite3"
    settings = {
        "review": {
            "provider": "mock",
            "deployment": "test",
            "profiles": {"standard": {}},
            "max_output_tokens": 100,
        }
    }
    s = Rooms(db, "owner", lambda: settings, {"mock": Reviewer()})
    other = Rooms(db, "other-owner", dict, {})
    try:
        room = room_args(s.create_room(uid(), "Demo", "Decide"))
        private = room_args(s.create_room(uid(), "Uninvited", "Private"))
        rid = room["room_id"]
        guest = DesktopParticipant(s)
        s.invite_claude(uid(), **room)
        with pytest.raises(RoomError, match="access_denied"):
            guest.read_messages(private["room_id"])
        assert DesktopParticipant(other).list_rooms()["rooms"] == []
        with pytest.raises(RoomError, match="access_denied"):
            DesktopParticipant(other).read_messages(rid)
        reviewer = s.create_agent(uid(), "Reviewer", "review", "standard", "Review")
        s.invite_participant(uid(), **room, agent_id=reviewer["agent_id"], handle="reviewer")
        note = s.send_message(uid(), **room, body="@claude in text only")["message"]
        with pytest.raises(RoomError, match="not_addressed"):
            guest.reply_to_mention(rid, note["message_id"], "unsolicited")
        sent = s.send_message(
            uid(), **room, body="Use atomic commits", kind="proposal", mentions=["@claude", "@reviewer"]
        )
        trigger = sent["message"]["message_id"]
        assert asyncio.run(Worker(s).once())
        assert not asyncio.run(Worker(s).once())
        with pytest.raises(RoomError, match="agreement_unverified"):
            s.close_room(uid(), **room, outcome="agreement", reason="missing vote", proposal_id=trigger)
        guest.reply_to_mention(rid, trigger, "Endorsed", kind="endorsement")
        assert (
            len(
                s.close_room(uid(), **room, outcome="agreement", reason="Agreed", proposal_id=trigger)[
                    "evidence"
                ]
            )
            == 3
        )
    finally:
        s.db.close()
        other.db.close()


def test_upgrade_existing_transcript(tmp_path):
    db = tmp_path / "old.sqlite3"
    schema = Path(__file__).resolve().parents[1] / "agent_room" / "schema.sql"
    before = schema.read_text().split("CREATE TABLE IF NOT EXISTS desktop_members")[0]
    old = sqlite3.connect(db)
    old.executescript(before + "PRAGMA user_version=1;")
    old.execute(
        "INSERT INTO rooms(id,owner,chair,proof,title,objective,limits) VALUES(?,?,?,?,?,?,?)",
        ("old", "owner", "chair", digest("proof"), "old room", "Review", "{}"),
    )
    old.execute("INSERT INTO members VALUES('old','chair','codex','session')")
    old.execute(
        "INSERT INTO messages VALUES('original','old',1,'chair',NULL,'discussion',?)",
        (json.dumps({"message_id": "original", "seq": 1}),),
    )
    old.commit()
    old.close()
    s = Rooms(db, "owner", dict, {})
    try:
        s.invite_claude(uid(), "old", "proof")
        assert DesktopParticipant(s).read_messages("old")["messages"][0]["message_id"] == "original"
        assert s.one("PRAGMA user_version")[0] == 2
    finally:
        s.db.close()


def test_desktop_turn_limits_and_delayed_reply(tmp_path):
    now = [1000]
    s = Rooms(tmp_path / "chat.sqlite3", "owner", dict, {}, clock=lambda: now[0])
    try:
        guest = DesktopParticipant(s)
        room = room_args(
            s.create_room(
                uid(), "Review", "Decide", {"max_rounds": 2, "max_messages": 3, "max_wall_seconds": 10}
            )
        )
        rid = room["room_id"]
        s.invite_claude(uid(), **room)
        first, second = [
            s.send_message(uid(), **room, body=text, mentions=["@claude"])["message"]["message_id"]
            for text in ("First question", "Second question")
        ]
        with pytest.raises(RoomError, match="max_rounds"):
            s.send_message(uid(), **room, body="Third round", mentions=["@claude"])
        answered = guest.reply_to_mention(rid, first, "First reply", mentions=["@claude"])
        assert answered["message"]["round_no"] == 1 and answered["deliveries"][0]["status"] == "suppressed"
        assert asyncio.run(guest.wait_for_mention(rid, 0))["message"]["message_id"] == second
        with pytest.raises(RoomError, match="max_messages"):
            guest.reply_to_mention(rid, second, "No space")
        now[0] += 11
        assert asyncio.run(guest.wait_for_mention(rid, 0)) == {"status": "deadline"}
        with pytest.raises(RoomError, match="deadline"):
            guest.reply_to_mention(rid, second, "Too late")
    finally:
        s.db.close()
