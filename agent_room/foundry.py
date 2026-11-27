"""Azure OpenAI Chat Completions and Responses transports."""

import json
from decimal import Decimal, InvalidOperation
from typing import ClassVar
from urllib.parse import quote, urlsplit

import httpx
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential

from .contracts import Prepared, Result, RoomError
from .core import require
from .discussion import discussion_messages

ZERO = {"input_tokens": 0, "output_tokens": 0}
CHAT_PROVIDER = "azure_openai_chat"
RESPONSES_PROVIDER = "azure_openai_responses"


def rates(config):
    card = config.get("pricing")
    if card is None:
        return None
    try:
        require(card["currency"] == "USD" and card["revision"], "invalid_price_card")
        values = [Decimal(str(card[k])) for k in ("input_per_million", "output_per_million")]
        values.append(
            Decimal(str(card["cached_input_per_million"]))
            if "cached_input_per_million" in card
            else values[0]
        )
        require(all(v.is_finite() and v >= 0 for v in values), "invalid_price_card")
    except (InvalidOperation, KeyError):
        raise RoomError("invalid_price_card") from None
    return values


def parse_usage(raw, *, responses=False):
    if not isinstance(raw, dict):
        return None
    input_key, output_key = ("input_tokens", "output_tokens") if responses else ("prompt_tokens", "completion_tokens")
    try:
        input_tokens, output_tokens = raw[input_key], raw[output_key]
        cached = (raw.get(f"{input_key}_details") or {}).get("cached_tokens", 0)
        reasoning = (raw.get(f"{output_key}_details") or {}).get("reasoning_tokens", 0)
    except (KeyError, AttributeError):
        return None
    if not all(type(count) is int and count >= 0 for count in (input_tokens, output_tokens, cached, reasoning)):
        return None
    # Some Chat Completions deployments report reasoning separately from completion tokens.
    total = raw.get("total_tokens")
    if not responses and reasoning and type(total) is int and total == input_tokens + output_tokens + reasoning:
        output_tokens += reasoning
    if cached > input_tokens or reasoning > output_tokens:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached,
        "reasoning_tokens": reasoning,
    }


class Foundry:
    registry_defaults: ClassVar[dict] = {}

    def __init__(self, credential=None, client=None):
        self.credential = credential if credential is not None else DefaultAzureCredential()
        self.client = client if client is not None else httpx.AsyncClient(follow_redirects=False)

    def validate(self, c, profile):
        origin = urlsplit(c["origin"])
        require(
            origin.scheme == "https"
            and origin.hostname
            and origin.path in ("", "/")
            and not (origin.username or origin.password or origin.query or origin.fragment),
            "invalid_https_origin",
        )
        responses = c.get("provider") == RESPONSES_PROVIDER
        require(c["route"] in (("v1",) if responses else ("v1", "dated")), "invalid_route")
        require(c["route"] == "v1" or c.get("api_version"), "api_version_required")
        require(c.get("token_scope") and c.get("deployment"), "missing_scope_or_deployment")
        for k in ("context_tokens", "max_output_tokens"):
            require(type(c[k]) is int and c[k] > 0, "invalid_" + k)
        require(c["context_tokens"] > c["max_output_tokens"], "invalid_context_tokens")
        require(c.get("input_bound_policy") == "deployment_context_ceiling", "unsupported_input_bound_policy")
        output_fields = ("max_output_tokens",) if responses else ("max_tokens", "max_completion_tokens")
        require(c["output_cap_field"] in output_fields, "invalid_output_cap_field")
        opts = c["profiles"][profile]
        require(
            set(opts) <= {"description", "reasoning_effort", "json_mode", "instruction_role"},
            "unsupported_profile_setting",
        )
        require(opts.get("instruction_role", "system") in ("developer", "system"), "invalid_instruction_role")
        require(type(opts.get("json_mode", False)) is bool, "invalid_json_mode")
        if "reasoning_effort" in opts:
            require(
                isinstance(opts["reasoning_effort"], str) and opts["reasoning_effort"].strip(),
                "invalid_reasoning_effort",
            )
        rates(c)

    def prepare(self, c, profile, instructions, context, output_limit):
        opts = c["profiles"][profile]
        responses = c.get("provider") == RESPONSES_PROVIDER
        payload = {c["output_cap_field"]: output_limit}
        if c["route"] == "v1":
            payload["model"] = c["deployment"]
        selected, truncated = discussion_messages(
            instructions, context, opts.get("instruction_role", "system"), c["context_tokens"] - output_limit
        )
        if responses:
            payload.update(input=selected, store=False)
            if "reasoning_effort" in opts:
                payload["reasoning"] = {"effort": opts["reasoning_effort"]}
            if opts.get("json_mode"):
                payload["text"] = {"format": {"type": "json_object"}}
        else:
            payload["messages"] = selected
            if "reasoning_effort" in opts:
                payload["reasoning_effort"] = opts["reasoning_effort"]
            if opts.get("json_mode"):
                payload["response_format"] = {"type": "json_object"}
        pricing = rates(c)
        bound = c["context_tokens"]
        maximum = None
        if pricing:
            input_rate, output_rate, cached_rate = pricing
            maximum = (bound * max(input_rate, cached_rate) + output_limit * output_rate) / 1_000_000
        return Prepared(payload, bound, output_limit, maximum, truncated)

    async def execute(self, c, turn):
        origin = c["origin"].rstrip("/")
        responses = c.get("provider") == RESPONSES_PROVIDER
        path = "/openai/v1/responses" if responses else "/openai/v1/chat/completions"
        query = {}
        if c["route"] == "dated":
            path = "/openai/deployments/" + quote(c["deployment"], safe="") + "/chat/completions"
            query["api-version"] = c["api_version"]
        try:
            token = await self.credential.get_token(c["token_scope"])
        except AzureError:
            return Result(None, ZERO, Decimal(0), "entra_authentication_failed")
        # The worker treats transport loss and ambiguous server errors as uncertain delivery.
        response = await self.client.post(
            origin + path,
            params=query,
            json=turn.payload,
            headers={"Authorization": "Bearer " + token.token},
            timeout=None,
        )
        request_id = response.headers.get("apim-request-id") or response.headers.get("x-request-id")
        if response.status_code in (400, 401, 403, 404, 422, 429):
            code = {
                400: "invalid_request",
                401: "authentication_failed",
                403: "inference_permission_denied",
                404: "deployment_not_found",
                422: "unsupported_profile",
                429: "throttled",
            }[response.status_code]
            return Result(None, ZERO, Decimal(0), code, request_id)
        response.raise_for_status()
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            return Result(None, None, None, "invalid_response", request_id)
        if not isinstance(data, dict):
            return Result(None, None, None, "invalid_response", request_id)
        usage = parse_usage(data.get("usage"), responses=responses)
        pricing = rates(c)
        cost = None
        if usage and pricing:
            inp, out, cached = pricing
            cost = (
                (usage["input_tokens"] - usage["cached_input_tokens"]) * inp
                + usage["cached_input_tokens"] * cached
                + usage["output_tokens"] * out
            ) / 1_000_000
        try:
            if responses:
                if data["status"] != "completed":
                    return Result(None, usage, cost, "incomplete_or_filtered_reply", request_id)
                parts = [part for item in data["output"] if item["type"] == "message" for part in item["content"]]
                if any(part["type"] == "refusal" for part in parts):
                    return Result(None, usage, cost, "incomplete_or_filtered_reply", request_id)
                content = "".join(part["text"] for part in parts if part["type"] == "output_text")
            else:
                choice = data["choices"][0]
                if choice["finish_reason"] != "stop":
                    return Result(None, usage, cost, "incomplete_or_filtered_reply", request_id)
                content = choice["message"]["content"]
            draft = json.loads(content)
        except (IndexError, KeyError, TypeError, ValueError):
            return Result(None, usage, cost, "invalid_draft", request_id)
        return Result(draft, usage, cost, request_id=request_id)

    async def close(self):
        await self.client.aclose()
        await self.credential.close()
