"""SQLite-backed room operations. Each public mutation uses one write transaction."""

import asyncio
import inspect
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from .contracts import Adapter, RoomError


def uid():
    return str(uuid4())


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(dump(value).encode()).hexdigest()


def require(condition, error):
    if not condition:
        raise RoomError(error)


ROOM = {"max_rounds": 3, "max_depth": 1, "max_wall_seconds": 300, "max_messages": 16, "max_call_seconds": 60}
MEMBER = {"max_turns": 16, "max_messages": 16, "max_call_seconds": 60, "max_output_tokens": 1024}
CEILINGS = {
    "max_rounds": 100,
    "max_depth": 8,
    "max_wall_seconds": 86400,
    "max_messages": 1000,
    "max_call_seconds": 300,
    "max_turns": 100,
    "max_output_tokens": 131072,
}


def policy(defaults, supplied, external=False, operator=None):
    operator = operator or {}
    values = defaults | operator | (supplied or {})
    require(
        set(values) <= set(defaults) | {"max_total_tokens", "max_estimated_usd", "inference_scope"},
        "invalid_limit",
    )
    for key in defaults:
        require(
            type(values[key]) is int and 0 < values[key] <= CEILINGS[key],
            f"invalid_limit: {key} maximum {CEILINGS[key]}",
        )
    if "max_total_tokens" in values:
        require(
            type(values["max_total_tokens"]) is int and values["max_total_tokens"] > 0, "invalid_token_cap"
        )
    if "max_estimated_usd" in values:
        try:
            cap = Decimal(str(values["max_estimated_usd"]))
            require(cap.is_finite() and cap >= 0, "invalid_cost_cap")
        except InvalidOperation:
            raise RoomError("invalid_cost_cap") from None
        values["max_estimated_usd"] = str(cap)
    scope = values.setdefault("inference_scope", "managed")
    require(scope in ("managed", "all_participants"), "invalid_scope")
    if external and scope == "all_participants":
        require(
            not {"max_total_tokens", "max_estimated_usd"}.intersection(values), "external_usage_unavailable"
        )
    for key, cap in operator.items():
        if key == "inference_scope":
            require(scope == cap or cap == "managed", "operator_inference_scope")
        else:
            require(Decimal(str(values[key])) <= Decimal(str(cap)), f"operator_limit: {key} maximum {cap}")
    return values


def transaction(method):
    signature = inspect.signature(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        data = dict(bound.arguments)
        del data["self"]
        with self.atomic():
            if "room_id" in data and data["room_id"] is not None:
                self.authorize(data["room_id"], data["room_proof"])
            if "request_id" not in data:
                return method(self, *args, **kwargs)
            key = data["request_id"]
            try:
                require(UUID(key).version == 4, "request_id_requires_uuid4")
            except (ValueError, TypeError, AttributeError):
                raise RoomError("request_id_requires_uuid4") from None
            identity = (self.owner, method.__name__, key)
            fingerprint = digest(data)
            prior = self.one("SELECT * FROM receipts WHERE owner=? AND tool=? AND key=?", identity)
            if prior:
                require(prior["digest"] == fingerprint, "idempotency_conflict")
                return json.loads(prior["result"])
            result = method(self, *args, **kwargs)
            self.db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (*identity, fingerprint, dump(result)))
            return result

    return wrapped


class Rooms:
    def __init__(self, path, owner, registry, adapters: dict[str, Adapter], clock=time.time, operator=None):
        self.owner, self.registry, self.adapters, self.clock = owner, registry, adapters, clock
        self.lock = threading.RLock()
        self.operator = operator or {}
        require(set(self.operator) <= {"room", "member"}, "invalid_operator_policy")
        policy(ROOM, None, external=True, operator=self.operator.get("room"))
        policy(MEMBER, None, operator=self.operator.get("member"))
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        require(self.db.execute("PRAGMA user_version").fetchone()[0] <= 2, "unsupported_database_version")
        schema = Path(__file__).with_name("schema.sql").read_text()
        self.db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")

    @contextmanager
    def atomic(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def authorize(self, room_id, proof):
        room = self.one("SELECT * FROM rooms WHERE id=? AND owner=?", (room_id, self.owner))
        require(room and secrets.compare_digest(room["proof"], digest(proof)), "room_access_denied")
        return room

    def status(self, room):
        if room["closure"]:
            return "closed"
        if room["deadline"] is not None and self.clock() >= room["deadline"]:
            return "deadline"
        return "open"

    def room(self, room_id):
        return self.one("SELECT * FROM rooms WHERE id=?", (room_id,))

    def active(self, room_id):
        room = self.room(room_id)
        status = self.status(room)
        require(status == "open", "room_" + status)
        return room

    def model(self, alias, profile):
        config = self.registry().get(alias)
        require(config and config.get("enabled", True), "deployment_unavailable")
        require(profile in config["profiles"], "unsupported_profile")
        self.adapters[config["provider"]].validate(config, profile)
        return config

    def list_models(self, provider: str | None = None) -> dict:
        """Discover authorized deployment aliases and settings profiles."""
        return {
            "models": [
                {
                    "alias": k,
                    "provider": v["provider"],
                    "deployment": v["deployment"],
                    "profiles": v["profiles"],
                    "priced": bool(v.get("pricing")),
                }
                for k, v in self.registry().items()
                if v.get("enabled", True) and (provider is None or provider == v["provider"])
            ]
        }

    @transaction
    def create_room(self, request_id: str, title: str, objective: str, limits: dict | None = None) -> dict:
        """Create a room and privately retain its room_proof for further calls."""
        require(title.strip() and objective.strip(), "missing_objective_or_title")
        room, chair, proof = uid(), uid(), secrets.token_urlsafe(32)
        settings = policy(ROOM, limits, external=True, operator=self.operator.get("room"))
        self.db.execute(
            "INSERT INTO rooms(id,owner,chair,proof,title,objective,limits) VALUES(?,?,?,?,?,?,?)",
            (room, self.owner, chair, digest(proof), title, objective, dump(settings)),
        )
        self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (room, chair, "codex", uid()))
        return {
            "room_id": room,
            "chair_id": chair,
            "handle": "codex",
            "room_proof": proof,
            "limits": settings,
            "cursor": 0,
        }

    @transaction
    def create_agent(
        self,
        request_id: str,
        name: str,
        alias: str,
        profile: str,
        instructions: str,
        limits: dict | None = None,
    ) -> dict:
        """Create an independent agent from an authorized deployment and profile."""
        config = self.model(alias, profile)
        require(name.strip() and instructions.strip(), "missing_agent_name_or_instructions")
        defaults = MEMBER | {"max_output_tokens": min(1024, config["max_output_tokens"])}
        operator = dict(self.operator.get("member", {}))
        if "max_output_tokens" in operator:
            operator["max_output_tokens"] = min(operator["max_output_tokens"], config["max_output_tokens"])
        settings = policy(defaults, limits, operator=operator)
        require(settings["max_output_tokens"] <= config["max_output_tokens"], "deployment_output_limit")
        agent, revision = uid(), digest(config)
        self.db.execute(
            "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                agent,
                self.owner,
                name,
                alias,
                profile,
                revision,
                instructions,
                dump(settings),
                config["provider"],
                config["deployment"],
            ),
        )
        return {
            "agent_id": agent,
            "revision": revision,
            "alias": alias,
            "profile": profile,
            "limits": settings,
        }

    @transaction
    def invite_participant(
        self, request_id: str, room_id: str, room_proof: str, agent_id: str, handle: str
    ) -> dict:
        """Invite an owned managed participant. Invitation alone does not run it."""
        self.active(room_id)
        handle = handle.removeprefix("@").lower()
        require(handle != "codex" and re.fullmatch("[a-z][a-z0-9_-]{0,63}", handle), "invalid_handle")
        require(
            self.one("SELECT id FROM agents WHERE id=? AND owner=?", (agent_id, self.owner)), "unknown_agent"
        )
        member = self.one(
            "SELECT * FROM members WHERE room=? AND (agent=? OR handle=?)", (room_id, agent_id, handle)
        )
        if member:
            require(member["agent"] == agent_id and member["handle"] == handle, "membership_conflict")
            session = member["session"]
        else:
            session = uid()
            self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (room_id, agent_id, handle, session))
        return {"participant_id": agent_id, "session_id": session, "handle": handle}

    def members(self, room_id):
        return self.db.execute("SELECT * FROM members WHERE room=? ORDER BY rowid", (room_id,)).fetchall()

    @transaction
    def invite_claude(self, request_id: str, room_id: str, room_proof: str) -> dict:
        """Grant your Claude Desktop connection transcript access and an @claude seat in this room."""
        self.active(room_id)
        member = self.one("SELECT agent FROM desktop_members WHERE room=?", (room_id,))
        if member:
            participant = member["agent"]
        else:
            require(
                not self.one("SELECT 1 FROM members WHERE room=? AND handle='claude'", (room_id,)),
                "membership_conflict: @claude is in use",
            )
            participant = uid()
            self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (room_id, participant, "claude", uid()))
            self.db.execute("INSERT INTO desktop_members VALUES(?,?)", (room_id, participant))
        return {"room_id": room_id, "participant_id": participant, "handle": "claude", "mode": "external"}

    @transaction
    def list_participants(self, room_id: str, room_proof: str) -> dict:
        """List public identities, handles, execution profiles and session status."""
        return self.roster(room_id)

    def roster(self, room_id):
        result = []
        for m in self.members(room_id):
            agent = self.one("SELECT * FROM agents WHERE id=?", (m["agent"],))
            desktop = not agent and m["agent"] != self.room(room_id)["chair"]
            job = self.one(
                "SELECT status,expires FROM jobs WHERE room=? AND agent=? AND status IN "
                "('dispatching','outcome_unknown')",
                (room_id, m["agent"]),
            )
            status = "idle" if not job else job["status"]
            if status == "dispatching" and job["expires"] <= self.clock():
                status = "outcome_unknown"
            result.append(
                {
                    "participant_id": m["agent"],
                    "handle": m["handle"],
                    "session_id": m["session"],
                    "mode": "managed" if agent else "external",
                    "alias": agent["alias"] if agent else m["handle"],
                    "provider": agent["provider"]
                    if agent
                    else ("claude_desktop" if desktop else "codex_host"),
                    "deployment": agent["deployment"] if agent else "unknown",
                    "name": agent["name"] if agent else ("Claude" if desktop else "Codex"),
                    "profile": agent["profile"] if agent else "unknown",
                    "status": status,
                    "limits": json.loads((agent or self.room(room_id))["limits"]),
                }
            )
        return {"participants": result}

    def slot(self, room, sender=None):
        clause = "room=?" + (" AND sender=?" if sender else "")
        params = (room, sender) if sender else (room,)
        used = self.one("SELECT COUNT(*) FROM messages WHERE " + clause, params)[0]
        pending = self.one(
            "SELECT COUNT(*) FROM jobs WHERE status='dispatching' AND " + clause.replace("sender", "agent"),
            params,
        )[0]
        return used + pending

    def append(self, room, sender, body, kind, parent, mentions, round_no, depth):
        rid = room["id"]
        require(isinstance(body, str) and body.strip(), "invalid_body")
        try:
            require(len(body.encode()) <= 65536, "invalid_body")
        except UnicodeEncodeError:
            raise RoomError("invalid_utf8_body") from None
        require(kind in ("discussion", "proposal", "endorsement", "dissent"), "invalid_kind")
        ref = self.one("SELECT kind FROM messages WHERE room=? AND id=?", (rid, parent))
        require(parent is None or ref, "unknown_parent")
        require(
            kind not in ("endorsement", "dissent") or (ref and ref["kind"] == "proposal"), "proposal_required"
        )
        require(isinstance(mentions, list) and all(isinstance(x, str) for x in mentions), "invalid_mentions")
        handles = {}
        for m in self.members(rid):
            handles["@" + m["handle"]] = handles[m["agent"]] = m["agent"]
        require(all(x in handles for x in mentions), "unknown_recipient")
        recipients = list(dict.fromkeys(handles[x] for x in mentions))
        limits = json.loads(room["limits"])
        require(self.slot(rid) < limits["max_messages"], "limit: max_messages")
        if sender == room["chair"] and any(x != sender for x in recipients):
            round_no = room["round"] + 1
            require(round_no <= limits["max_rounds"], "limit: max_rounds")
            self.db.execute(
                "UPDATE rooms SET round=?,deadline=COALESCE(deadline,?) WHERE id=?",
                (round_no, self.clock() + limits["max_wall_seconds"], rid),
            )
        seq = self.one("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE room=?", (rid,))[0]
        mid = uid()
        envelope = {
            "schema_version": 1,
            "message_id": mid,
            "room_id": rid,
            "seq": seq,
            "created_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "sender_id": sender,
            "body": body,
            "kind": kind,
            "reply_to_id": parent,
            "mentions": recipients,
            "round_no": round_no or None,
            "depth": depth,
        }
        self.db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
            (mid, rid, seq, sender, parent, kind, dump(envelope)),
        )
        deliveries = []
        for target in recipients:
            status, reason = "queued", None
            if target == room["chair"]:
                status = "notified"
            elif target == sender or depth + 1 > limits["max_depth"]:
                status, reason = "suppressed", "self_or_depth"
            elif self.one(
                "SELECT 1 FROM jobs WHERE room=? AND round=? AND agent=? AND status!='suppressed'",
                (rid, round_no, target),
            ):
                status, reason = "suppressed", "already_addressed"
            elif self.one("SELECT 1 FROM desktop_members WHERE room=? AND agent=?", (rid, target)):
                status = "notified"
            self.db.execute(
                "INSERT INTO jobs(id,room,trigger,agent,round,depth,status,reason,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid(), rid, mid, target, round_no or 0, depth + 1, status, reason, self.clock()),
            )
            deliveries.append({"participant_id": target, "status": status, "reason": reason})
        return {"message": envelope, "deliveries": deliveries}

    @transaction
    def send_message(
        self,
        request_id: str,
        room_id: str,
        room_proof: str,
        body: str,
        kind: str = "discussion",
        reply_to_id: str | None = None,
        mentions: list[str] | None = None,
    ) -> dict:
        """Post as the room chair. Use explicit @handles or IDs in mentions to activate participants."""
        room = self.active(room_id)
        return self.append(room, room["chair"], body, kind, reply_to_id, mentions or [], room["round"], 0)

    @transaction
    def read_messages(self, room_id: str, room_proof: str, after_seq: int = 0, page_size: int = 50) -> dict:
        """Read a page of the persistent transcript after an exclusive sequence cursor."""
        return self.transcript(room_id, after_seq, page_size)

    def transcript(self, room_id, after_seq, page_size):
        require(after_seq >= 0 and 1 <= page_size <= 200, "invalid_page")
        rows = self.db.execute(
            "SELECT envelope FROM messages WHERE room=? AND seq>? ORDER BY seq LIMIT ?",
            (room_id, after_seq, page_size + 1),
        ).fetchall()
        msgs = [json.loads(x[0]) for x in rows[:page_size]]
        room = self.room(room_id)
        return {
            "messages": msgs,
            "next_cursor": msgs[-1]["seq"] if msgs else after_seq,
            "more": len(rows) > page_size,
            "rounds": room["round"],
            "deadline": room["deadline"],
            "status": self.status(room),
            "closure": json.loads(room["closure"]) if room["closure"] else None,
        }

    async def wait_for_reply(
        self, room_id: str, room_proof: str, trigger_id: str, participant_id: str, timeout: float = 10
    ) -> dict:
        """Wait up to 20 seconds for a specific direct reply or terminal disposition."""
        require(0 <= timeout <= 20, "invalid_timeout")
        end = time.monotonic() + timeout
        while True:
            with self.atomic():
                room = self.authorize(room_id, room_proof)
                reply = self.one(
                    "SELECT envelope FROM messages WHERE room=? AND parent=? AND sender=? ORDER BY seq LIMIT 1",
                    (room_id, trigger_id, participant_id),
                )
                info = {"trigger_id": trigger_id, "participant_id": participant_id}
                if reply:
                    return dict(**info, status="reply", message=json.loads(reply[0]))
                job = self.one(
                    "SELECT * FROM jobs WHERE room=? AND trigger=? AND agent=?",
                    (room_id, trigger_id, participant_id),
                )
                if not job:
                    return dict(**info, status="not_addressed")
                if job["status"] not in ("queued", "waiting_budget", "dispatching", "notified"):
                    return dict(**info, status=job["status"], reason=job["reason"])
                if job["status"] == "dispatching" and job["expires"] <= self.clock():
                    return dict(**info, status="outcome_unknown")
                if self.status(room) != "open":
                    return dict(**info, status=self.status(room))
            remaining = end - time.monotonic()
            if remaining <= 0:
                return dict(**info, status="timeout")
            await asyncio.sleep(min(0.1, remaining))

    @transaction
    def close_room(
        self,
        request_id: str,
        room_id: str,
        room_proof: str,
        outcome: str,
        reason: str,
        proposal_id: str | None = None,
    ) -> dict:
        """Close a room. Agreement requires every member's assent to the same proposal."""
        room = self.room(room_id)
        require(not room["closure"], "room_closed")
        require(outcome in ("agreement", "no_agreement", "limit_reached", "stopped"), "invalid_outcome")
        evidence = []
        if outcome == "agreement":
            p = self.one(
                "SELECT * FROM messages WHERE room=? AND id=? AND kind='proposal'", (room_id, proposal_id)
            )
            members = self.members(room_id)
            require(p and len(members) >= 2, "agreement_unverified")
            evidence.append(proposal_id)
            for m in members:
                vote = self.one(
                    "SELECT id,kind FROM messages WHERE room=? AND sender=? AND parent=? "
                    "AND kind IN ('endorsement','dissent') ORDER BY seq DESC LIMIT 1",
                    (room_id, m["agent"], proposal_id),
                )
                if vote or m["agent"] != p["sender"]:
                    require(vote and vote["kind"] == "endorsement", "agreement_unverified")
                    evidence.append(vote["id"])
        from .usage import report

        closure = {
            "outcome": outcome,
            "reason": reason,
            "proposal_id": proposal_id,
            "evidence": evidence,
            "rounds": room["round"],
            "cursor": self.one("SELECT COUNT(*) FROM messages WHERE room=?", (room_id,))[0],
            "usage": report(self, room_id, None),
        }
        self.db.execute("UPDATE rooms SET closure=? WHERE id=?", (dump(closure), room_id))
        self.db.execute(
            "UPDATE jobs SET status='closed' WHERE room=? AND status IN ('queued','waiting_budget')",
            (room_id,),
        )
        return closure

    @transaction
    def get_usage(
        self, room_id: str | None = None, room_proof: str | None = None, participant_id: str | None = None
    ) -> dict:
        """Report measured inference, outstanding reservations and external usage coverage."""
        from .usage import report

        if room_id is None:
            require(
                self.one("SELECT id FROM agents WHERE owner=? AND id=?", (self.owner, participant_id)),
                "access_denied",
            )
        elif participant_id:
            require(
                self.one("SELECT 1 FROM members WHERE room=? AND agent=?", (room_id, participant_id)),
                "unknown_member",
            )
        return report(self, room_id, participant_id)
