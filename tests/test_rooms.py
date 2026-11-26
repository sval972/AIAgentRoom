import asyncio
from decimal import Decimal

import pytest

from agent_room.contracts import Prepared, Result, RoomError
from agent_room.core import Rooms, uid
from agent_room.worker import Worker


class Mock:
    def __init__(self):
        self.contexts = []
        self.drafts = []
        self.block = None

    def validate(self, config, profile):
        pass

    def prepare(self, config, profile, instructions, context, output_limit):
        return Prepared((instructions, context), 500, 100, Decimal("0.005"))

    async def execute(self, config, turn):
        self.contexts.append(turn.payload)
        if self.block:
            await self.block.wait()
        result = self.drafts.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, Result):
            return result
        kind, body, mentions = result
        return Result(
            {"kind": kind, "body": body, "mentions": mentions},
            {"input_tokens": 350, "output_tokens": 50},
            Decimal("0.001"),
        )


@pytest.fixture
def fixture(tmp_path):
    adapter = Mock()
    config = {
        "provider": "mock",
        "deployment": "fixture",
        "profiles": {"standard": {}},
        "max_output_tokens": 512,
    }
    registry = {"review": config, "other": config.copy()}
    path = tmp_path / "rooms.sqlite3"

    def connect():
        return Rooms(path, "local", lambda: registry, {"mock": adapter})

    service = connect()
    yield service, adapter, registry, connect
    service.db.close()


def setup_room(s, limits=None):
    created = s.create_room(uid(), "Review", "Choose an import strategy", limits)
    return {key: created[key] for key in ("room_id", "room_proof")}


def invite(s, room, handle="critic"):
    agent = s.create_agent(uid(), handle, "review", "standard", "Find a failure case.")
    s.invite_participant(uid(), **room, agent_id=agent["agent_id"], handle=handle)
    return agent["agent_id"]


@pytest.mark.parametrize("limits", [{"max_total_tokens": 600}, {"max_estimated_usd": "0.005"}])
def test_missing_usage_remains_reserved(fixture, limits):
    s, m, _, _ = fixture
    room = setup_room(s, limits)
    a = invite(s, room)
    m.drafts = [Result(None, None, None, "invalid_response")]
    for _ in range(2):
        s.send_message(uid(), **room, body="Evaluate", mentions=[a])
        assert asyncio.run(Worker(s).once())
    stats = s.get_usage(**room)["total"]
    assert stats["reserved_tokens"] == 600 and Decimal(stats["reserved_usd"]) == Decimal("0.005")
    assert stats["message_count"] == 2 and len(m.contexts) == 1


def test_disagreement_and_new_proposal(fixture):
    s, m, _, _ = fixture
    room = setup_room(s)
    a = invite(s, room)
    b = invite(s, room, "peer")
    m.drafts = [("endorsement", "Agreed", []), ("dissent", "A crash could lose data.", [])]
    original = s.send_message(uid(), **room, body="Save later.", kind="proposal", mentions=[a, b])["message"]
    worker = Worker(s)
    assert asyncio.run(worker.once()) and asyncio.run(worker.once())
    revision = s.send_message(uid(), **room, body="Commit before acknowledgement.", kind="proposal")[
        "message"
    ]
    for p in (original, revision):
        with pytest.raises(RoomError, match="agreement_unverified"):
            s.close_room(uid(), **room, outcome="agreement", reason="check", proposal_id=p["message_id"])
    assert (
        s.close_room(uid(), **room, outcome="no_agreement", reason="Revision has no endorsements")["rounds"]
        == 1
    )


def test_session_serialization_and_cursor(fixture):
    s, m, _, connect = fixture
    room = setup_room(s)
    a = invite(s, room)
    for body in ("first", "second"):
        s.send_message(uid(), **room, body=body, mentions=[a])
    m.drafts = [("discussion", "first answer", []), ("discussion", "second answer", [])]
    second = connect()

    async def scenario():
        m.block = asyncio.Event()
        task = asyncio.create_task(Worker(s).once())
        while not m.contexts:
            await asyncio.sleep(0.001)
        assert not await Worker(second).once()
        m.block.set()
        await task
        assert await Worker(second).once()

    try:
        asyncio.run(scenario())
    finally:
        second.db.close()
    page = s.read_messages(**room, page_size=2)
    tail = s.read_messages(**room, after_seq=page["next_cursor"], page_size=2)
    assert page["more"] and not tail["more"]
    assert [x["seq"] for x in page["messages"] + tail["messages"]] == [1, 2, 3, 4]


def test_three_round_agreement_and_persistence(fixture):
    s, m, _, connect = fixture
    room = setup_room(s)
    agent = invite(s, room)
    m.drafts = [
        ("discussion", "Concurrent writers can race.", ["@codex"]),
        ("discussion", "A transaction can roll back both writes.", ["@codex"]),
        ("endorsement", "I agree with the atomic write.", ["@codex"]),
    ]
    for i, body in enumerate(
        ("Check the ID first.", "What if the process crashes?", "Commit the record and ID atomically.")
    ):
        req = dict(
            request_id=uid(),
            **room,
            body=body,
            mentions=["@critic", "@critic"],
            kind="proposal" if i == 2 else "discussion",
        )
        receipt = s.send_message(**req)
        assert s.send_message(**req) == receipt
        trigger = receipt["message"]["message_id"]
        assert asyncio.run(Worker(s).once())
        reply = asyncio.run(s.wait_for_reply(**room, trigger_id=trigger, participant_id=agent, timeout=0))
        assert reply["message"]["round_no"] == i + 1
        assert len(m.contexts[-1][1]["history"]) == i * 2 + 1
    with pytest.raises(RoomError, match="max_rounds"):
        s.send_message(uid(), **room, body="Again", mentions=["@critic"])
    closure = s.close_room(uid(), **room, outcome="agreement", reason="Both accepted", proposal_id=trigger)
    assert len(closure["evidence"]) == 2
    reopened = connect()
    try:
        transcript = reopened.read_messages(**room)
        assert transcript["closure"] == closure and len(transcript["messages"]) == 6
        stats = reopened.get_usage(**room)
        assert stats["total"]["input_tokens"] == 1050
        assert stats["participants"][0]["estimated_cost_usd"] is None
    finally:
        reopened.db.close()


def test_isolation_limits_and_authority(fixture):
    s, m, registry, _ = fixture
    room = setup_room(s, {"max_total_tokens": 599})
    agent = invite(s, room)
    assert s.send_message(uid(), **room, body="@critic quoted")["deliveries"] == []
    assert not asyncio.run(Worker(s).once())
    key = uid()
    trigger = s.send_message(key, **room, body="proposal", kind="proposal", mentions=[agent])["message"][
        "message_id"
    ]
    with pytest.raises(RoomError, match="idempotency_conflict"):
        s.send_message(key, **room, body="changed")
    asyncio.run(Worker(s).once())
    reply = asyncio.run(s.wait_for_reply(**room, trigger_id=trigger, participant_id=agent, timeout=0))
    assert "max_total_tokens" in reply["reason"] and not m.contexts
    with pytest.raises(RoomError, match="agreement_unverified"):
        s.close_room(uid(), **room, outcome="agreement", reason="guess", proposal_id=trigger)
    with pytest.raises(RoomError, match="access_denied"):
        s.read_messages(room["room_id"], "wrong")
    other = setup_room(s)
    with pytest.raises(RoomError, match="unknown_parent"):
        s.send_message(uid(), **other, body="reply", reply_to_id=trigger)
    registry["review"]["enabled"] = False
    with pytest.raises(RoomError, match="unavailable"):
        invite(s, other)


def test_parallel_sessions_and_fencing(fixture):
    s, m, _, connect = fixture
    room = setup_room(s, {"max_total_tokens": 1000})
    a, b = invite(s, room), invite(s, room, "peer")
    s.send_message(uid(), **room, body="review", mentions=[a, b])
    second = connect()

    async def scenario():
        m.block = asyncio.Event()
        m.drafts = [("discussion", "I have an objection.", [])]
        pending = asyncio.create_task(Worker(s).once())
        while not m.contexts:
            await asyncio.sleep(0.001)
        assert await Worker(second).once()
        assert len(m.contexts) == 1
        m.block.set()
        await pending
        m.drafts.append(RuntimeError())
        await Worker(second).once()
        s.send_message(uid(), **room, body="new round", mentions=[b])
        await Worker(s).once()
        assert len(m.contexts) == 2
        assert s.get_usage(**room)["total"]["reserved_tokens"] == 600

    try:
        asyncio.run(scenario())
    finally:
        second.db.close()


def test_delegation_and_cycle(fixture):
    s, m, _, _ = fixture
    room = setup_room(s, {"max_depth": 3})
    invite(s, room)
    invite(s, room, "peer")
    m.drafts = [
        ("discussion", "Please challenge this.", ["@peer"]),
        ("discussion", "Here is a challenge.", ["@critic"]),
    ]
    s.send_message(uid(), **room, body="Start", mentions=["@critic"])
    worker = Worker(s)
    assert asyncio.run(worker.once()) and asyncio.run(worker.once())
    assert not asyncio.run(worker.once())
    transcript = s.read_messages(**room)["messages"]
    assert [x["depth"] for x in transcript] == [0, 1, 2]
    assert {x["round_no"] for x in transcript} == {1}


def test_restart_after_dispatch_quarantines_only_that_session(fixture):
    s, m, _, connect = fixture
    first, other = setup_room(s), setup_room(s)
    agent = invite(s, first)
    s.invite_participant(uid(), **other, agent_id=agent, handle="critic")
    trigger = s.send_message(uid(), **first, body="Private to first", mentions=[agent])["message"][
        "message_id"
    ]
    # The first process has committed its dispatch marker and disappears.
    assert Worker(s).claim()
    restarted = connect()
    try:
        restarted.clock = lambda: s.clock() + 65
        s.send_message(uid(), **other, body="Independent discussion", mentions=[agent])
        m.drafts = [("discussion", "Independent answer", [])]
        assert asyncio.run(Worker(restarted).once())
        assert len(m.contexts[0][1]["history"]) == 1
        assert m.contexts[0][1]["history"][0]["body"] == "Independent discussion"
        status = asyncio.run(
            restarted.wait_for_reply(**first, trigger_id=trigger, participant_id=agent, timeout=0)
        )
        assert status["status"] == "outcome_unknown"
        assert restarted.get_usage(**first)["total"]["reserved_tokens"] == 600
    finally:
        restarted.db.close()


def test_late_answer_fenced_and_metered(fixture):
    s, m, _, _ = fixture
    room = setup_room(s)
    agent = invite(s, room)
    s.send_message(uid(), **room, body="question", mentions=[agent])
    m.drafts = [("discussion", "late answer", [])]

    async def scenario():
        m.block = asyncio.Event()
        pending = asyncio.create_task(Worker(s).once())
        while not m.contexts:
            await asyncio.sleep(0.001)
        s.close_room(uid(), **room, outcome="stopped", reason="Done")
        m.block.set()
        await pending

    asyncio.run(scenario())
    assert len(s.read_messages(**room)["messages"]) == 1
    assert s.get_usage(**room)["total"]["input_tokens"] == 350


def test_revision_and_deadline(fixture):
    s, m, registry, _ = fixture
    now = [1000]
    s.clock = lambda: now[0]
    room = setup_room(s, {"max_wall_seconds": 4})
    a = invite(s, room)
    trigger = s.send_message(uid(), **room, body="Check", mentions=[a])["message"]["message_id"]
    registry["review"]["deployment"] = "different"
    assert asyncio.run(Worker(s).once())
    result = asyncio.run(s.wait_for_reply(**room, trigger_id=trigger, participant_id=a, timeout=0))
    assert result["reason"] == "deployment_revision_changed"
    now[0] += 5
    with pytest.raises(RoomError, match="room_deadline"):
        s.send_message(uid(), **room, body="one more")
    assert s.read_messages(**room)["status"] == "deadline" and not m.contexts


def test_operator_policy_and_missing_external_metrics(tmp_path):
    s = Rooms(
        tmp_path / "limits.sqlite3", "owner", dict, {}, operator={"room": {"max_estimated_usd": "0.10"}}
    )
    try:
        with pytest.raises(RoomError, match="operator_limit"):
            setup_room(s, {"max_estimated_usd": "0.11"})
        with pytest.raises(RoomError, match="external_usage_unavailable"):
            setup_room(s, {"inference_scope": "all_participants"})
        assert s.get_usage(**setup_room(s))["total"]["remaining_room_budget"]["usd"] == "0.10"
    finally:
        s.db.close()


def test_mock_conversation_through_mcp(fixture):
    from fastmcp import Client

    from agent_room.server import build_server

    s, m, _, _ = fixture
    m.drafts = [("endorsement", "The claim holds under the given assumptions.", ["@codex"])]

    async def scenario():
        async with Client(build_server(s)) as client:

            async def call(tool, **args):
                return (await client.call_tool(tool, args)).data

            created = await call("create_room", request_id=uid(), title="MCP", objective="Reach a decision")
            room = {k: created[k] for k in ("room_id", "room_proof")}
            agent = await call(
                "create_agent",
                request_id=uid(),
                name="critic",
                alias="review",
                profile="standard",
                instructions="Assess the proposal",
            )
            await call(
                "invite_participant", **room, request_id=uid(), agent_id=agent["agent_id"], handle="critic"
            )
            sent = await call(
                "send_message",
                **room,
                request_id=uid(),
                body="Use a transaction.",
                kind="proposal",
                mentions=["@critic"],
            )
            trigger = sent["message"]["message_id"]
            result = await call(
                "wait_for_reply", **room, trigger_id=trigger, participant_id=agent["agent_id"], timeout=5
            )
            assert result["status"] == "reply"
            closed = await call(
                "close_room",
                **room,
                request_id=uid(),
                outcome="agreement",
                reason="Both agreed",
                proposal_id=trigger,
            )
            assert closed["evidence"] == [trigger, result["message"]["message_id"]]

    asyncio.run(scenario())
