"""Provider-neutral turn and accounting contracts."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class RoomError(ValueError):
    """Safe, user-visible error code and detail."""


@dataclass(frozen=True)
class Prepared:
    payload: Any
    input_bound: int
    output_bound: int
    reserved_usd: Decimal | None
    truncated: bool = False


@dataclass
class Result:
    draft: dict | None
    usage: dict | None
    cost: Decimal | None
    error: str | None = None
    request_id: str | None = None


class Adapter(Protocol):
    def validate(self, config: dict, profile: str) -> None: ...
    def prepare(
        self, config: dict, profile: str, instructions: str, context: dict, output_limit: int
    ) -> Prepared: ...
    async def execute(self, config: dict, prepared: Prepared) -> Result: ...
