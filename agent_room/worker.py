"""Durable dispatch. SQLite commits the reservation before any provider I/O."""

import asyncio
import json
import logging
import sqlite3
import time
from decimal import Decimal

from .contracts import Result, RoomError
from .core import digest, dump, require, uid
from .usage import ledgers, remaining, summarize

logger = logging.getLogger(__name__)


def admission(service, room, agent, settings, prepared):
    for pid, limits in ((None, json.loads(room["limits"])), (agent, settings)):
        stats = summarize(ledgers(service, room["id"], pid))
        if pid:
            require(stats["turns"] < settings["max_turns"], "limit: max_turns")
        require(service.slot(room["id"], pid) < limits["max_messages"], "limit: max_messages")
        budget = remaining(limits, stats)
        if "tokens" in budget:
            require(prepared.input_bound is not None, "provider_has_no_hard_token_bound")
            require(
                prepared.input_bound + prepared.output_bound <= budget["tokens"], "limit: max_total_tokens"
            )
        if "usd" in budget:
            require(prepared.reserved_usd is not None, "unpriced_deployment")
            require(prepared.reserved_usd <= Decimal(budget["usd"]), "limit: max_estimated_usd")


class Worker:
    def __init__(self, service):
        self.s = service

    def claim(self):
        s = self.s
        with s.atomic():
            expired = s.db.execute(
                "UPDATE jobs SET status='outcome_unknown',reason='dispatch_expired' "
                "WHERE status='dispatching' AND expires<=?",
                (s.clock(),),
            )
            if expired.rowcount:
                s.db.execute("UPDATE jobs SET status='queued' WHERE status='waiting_budget'")
            job = s.one(
                "SELECT j.* FROM jobs j JOIN rooms r ON r.id=j.room WHERE r.owner=? AND j.status='queued' "
                "AND NOT EXISTS(SELECT 1 FROM jobs b WHERE b.room=j.room AND b.agent=j.agent "
                "AND (b.status='dispatching' OR (b.status='waiting_budget' AND b.rowid<j.rowid))) "
                "ORDER BY j.rowid LIMIT 1",
                (s.owner,),
            )
            if not job:
                return None
            try:
                room = s.active(job["room"])
                require(
                    not s.one(
                        "SELECT 1 FROM jobs WHERE room=? AND agent=? AND status='outcome_unknown'",
                        (job["room"], job["agent"]),
                    ),
                    "session_quarantined",
                )
                agent = s.one("SELECT * FROM agents WHERE id=?", (job["agent"],))
                settings = json.loads(agent["limits"])
                config = s.model(agent["alias"], agent["profile"])
                require(digest(config) == agent["revision"], "deployment_revision_changed")
                history = [
                    json.loads(x[0])
                    for x in s.db.execute(
                        "SELECT envelope FROM messages WHERE room=? ORDER BY seq", (job["room"],)
                    )
                ]
                context = {
                    "history": history,
                    "objective": room["objective"],
                    "trigger_id": job["trigger"],
                    "participant_id": job["agent"],
                    "members": {m["handle"]: m["agent"] for m in s.members(job["room"])},
                }
                adapter = s.adapters[config["provider"]]
                prepared = adapter.prepare(
                    config, agent["profile"], agent["instructions"], context, settings["max_output_tokens"]
                )
                admission(s, room, job["agent"], settings, prepared)
                timeout = min(
                    settings["max_call_seconds"],
                    json.loads(room["limits"])["max_call_seconds"],
                    room["deadline"] - s.clock(),
                )
                require(timeout > 0, "room_deadline")
            except (RoomError, OSError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, RoomError) else "invalid_registry"
                waiting = reason in {
                    "limit: max_total_tokens",
                    "limit: max_estimated_usd",
                    "limit: max_messages",
                } and s.one("SELECT 1 FROM jobs WHERE room=? AND status='dispatching'", (job["room"],))
                status = "waiting_budget" if waiting else "failed"
                s.db.execute("UPDATE jobs SET status=?,reason=? WHERE id=?", (status, reason, job["id"]))
                return False
            fence = uid()
            ledger = {
                "reserved_tokens": (
                    prepared.input_bound + prepared.output_bound if prepared.input_bound is not None else None
                ),
                "input_bound": prepared.input_bound,
                "output_bound": prepared.output_bound,
                "reserved_usd": str(prepared.reserved_usd) if prepared.reserved_usd is not None else None,
                "revision": agent["revision"],
                "price_revision": config.get("pricing", {}).get("revision"),
                "high_water_seq": history[-1]["seq"],
                "truncated": prepared.truncated,
                "queue_ms": int(1000 * (s.clock() - job["created"])),
            }
            s.db.execute(
                "UPDATE jobs SET status='dispatching',attempt=?,expires=?,ledger=? WHERE id=?",
                (fence, s.clock() + timeout, dump(ledger), job["id"]),
            )
            return job, fence, adapter, config, prepared, timeout, ledger

    async def once(self):
        claim = self.claim()
        if not claim:
            return claim is False
        job, fence, adapter, config, prepared, timeout, ledger = claim
        started = time.monotonic()
        call = asyncio.create_task(adapter.execute(config, prepared))
        try:
            result = None
            while time.monotonic() - started < timeout:
                done, _ = await asyncio.wait({call}, timeout=min(0.2, timeout - (time.monotonic() - started)))
                if done:
                    result = call.result()
                    break
                with self.s.atomic():
                    if self.s.status(self.s.room(job["room"])) != "open":
                        break
        except asyncio.CancelledError:
            self.finish(job, fence, ledger, None, started)
            raise
        except Exception as exc:  # noqa: BLE001 - adapter failure after dispatch has uncertain delivery
            logger.warning("Provider returned no confirmed result (%s)", type(exc).__name__)
            result = None
        finally:
            if not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
        self.finish(job, fence, ledger, result, started)
        return True

    def finish(self, job, fence, ledger, result: Result | None, started):
        s = self.s
        with s.atomic():
            current = s.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
            if current["attempt"] != fence or current["status"] not in ("dispatching", "outcome_unknown"):
                return
            ledger["latency_ms"] = round(1000 * (time.monotonic() - started))
            status, reason = "outcome_unknown", "delivery_uncertain"
            if result is not None:
                ledger.update(
                    usage=result.usage,
                    cost=str(result.cost) if result.cost is not None else None,
                    provider_request_id=result.request_id,
                )
                status, reason = "failed", result.error
                if (
                    result.usage
                    and ledger["input_bound"] is not None
                    and (
                        result.usage["input_tokens"] > ledger["input_bound"]
                        or result.usage["output_tokens"] > ledger["output_bound"]
                        or (
                            result.cost is not None
                            and ledger["reserved_usd"] is not None
                            and result.cost > Decimal(ledger["reserved_usd"])
                        )
                    )
                ):
                    reason = "accounting_bound_exceeded"
                    s.db.execute("UPDATE rooms SET deadline=? WHERE id=?", (s.clock(), job["room"]))
                room = s.room(job["room"])
                if s.status(room) != "open":
                    status = s.status(room)
                elif s.clock() >= current["expires"]:
                    status, reason = "failed", "call_deadline"
                elif reason is None:
                    # Convert this reserved message slot into a committed message.
                    s.db.execute("UPDATE jobs SET status='publishing' WHERE id=?", (job["id"],))
                    try:
                        draft = result.draft
                        require(
                            isinstance(draft, dict) and set(draft) == {"body", "kind", "mentions"},
                            "invalid_draft",
                        )
                        s.append(
                            room,
                            job["agent"],
                            draft["body"],
                            draft["kind"],
                            job["trigger"],
                            draft["mentions"],
                            job["round"],
                            job["depth"],
                        )
                        status = "completed"
                    except RoomError as exc:
                        reason = str(exc)
            s.db.execute(
                "UPDATE jobs SET status=?,reason=?,ledger=? WHERE id=?",
                (status, reason, dump(ledger), job["id"]),
            )
            s.db.execute(
                "UPDATE jobs SET status='queued',reason=NULL WHERE room=? AND status='waiting_budget'",
                (job["room"],),
            )

    async def run(self):
        while True:
            try:
                worked = await self.once()
            except sqlite3.OperationalError as exc:
                logger.warning("SQLite worker operation interrupted (%s)", type(exc).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(0.15)
