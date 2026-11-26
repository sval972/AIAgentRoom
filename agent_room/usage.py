"""Aggregate durable attempt ledgers using decimal USD amounts."""

import json
from decimal import Decimal


def summarize(ledgers):
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0}
    cost = Decimal(0)
    reserved = Decimal(0)
    tokens = missing = unpriced = 0
    times = []
    for entry in ledgers:
        actual = entry.get("usage")
        if actual is None:
            tokens += entry["reserved_tokens"]
            reserved += Decimal(entry["reserved_usd"] or 0)
            missing += 1
        else:
            for key in usage:
                usage[key] += actual.get(key, 0)
        amount = entry.get("cost") if actual is not None else entry["reserved_usd"]
        if amount is None:
            unpriced += 1
        elif actual is not None:
            cost += Decimal(amount)
        if "latency_ms" in entry:
            times.append(entry["latency_ms"])
    return dict(
        **usage,
        turns=len(ledgers),
        reserved_tokens=tokens,
        reserved_usd=str(reserved),
        measured_cost_usd=str(cost),
        estimated_cost_usd=None if unpriced or missing else str(cost),
        unknown_usage_calls=missing,
        unpriced_calls=unpriced,
        measured_calls=len(times),
        latency_sum_ms=sum(times),
        latency_mean_ms=sum(times) / len(times) if times else None,
        latency_max_ms=max(times, default=None),
    )


def ledgers(s, room, agent=None):
    where = "room=?" + (" AND agent=?" if agent else "")
    params = (room, agent) if agent else (room,)
    return [
        json.loads(x[0])
        for x in s.db.execute("SELECT ledger FROM jobs WHERE ledger IS NOT NULL AND " + where, params)
    ]


def remaining(policy, stats):
    result = {}
    if "max_total_tokens" in policy:
        result["tokens"] = (
            policy["max_total_tokens"]
            - stats["input_tokens"]
            - stats["output_tokens"]
            - stats["reserved_tokens"]
        )
    if "max_estimated_usd" in policy:
        result["usd"] = str(
            Decimal(policy["max_estimated_usd"])
            - Decimal(stats["measured_cost_usd"])
            - Decimal(stats["reserved_usd"])
        )
    return result


def report(s, room, agent):
    where = " WHERE room=?" if room else " WHERE agent=?"
    params = (room or agent,)
    if room and agent:
        where += " AND agent=?"
        params += (agent,)
    participants, all_entries = [], []
    count = 0
    for m in s.db.execute("SELECT * FROM members" + where, params):
        rid, pid = m["room"], m["agent"]
        entries = ledgers(s, rid, pid)
        stats = summarize(entries)
        external = pid == s.room(rid)["chair"]
        messages = s.one("SELECT COUNT(*) FROM messages WHERE room=? AND sender=?", (rid, pid))[0]
        count += messages
        if external:
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
                "estimated_cost_usd",
                "measured_cost_usd",
                "latency_sum_ms",
            ):
                stats[key] = None
        else:
            limits = json.loads(s.one("SELECT limits FROM agents WHERE id=?", (pid,))[0])
            stats["remaining"] = remaining(limits, stats)
        participants.append(
            dict(
                **stats,
                participant_id=pid,
                room_id=rid,
                message_count=messages,
                coverage="external_unavailable" if external else "managed",
            )
        )
        all_entries.extend(entries)
    total = summarize(all_entries)
    if room:
        total["remaining_room_budget"] = remaining(
            json.loads(s.room(room)["limits"]), summarize(ledgers(s, room))
        )
    return {
        "total": total | {"message_count": count},
        "participants": participants,
        "inference_coverage": "managed",
    }
