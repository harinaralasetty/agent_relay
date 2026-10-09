"""The provider-neutral runtime boundary."""

from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_TOOL_ROUNDS = 8
DEFAULT_OUTPUT_TOKENS = 512


@dataclass(frozen=True)
class RuntimeConfig:
    peer_id: str
    model: str
    cwd: str
    mcp_command: str
    mcp_args: list[str]
    mcp_env: dict[str, str]
    timeout: float = 90.0
    effort: str = "low"
    max_budget_usd: float = 0.25
    executable: str | None = None
    resume_session: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_tool_rounds: int = DEFAULT_TOOL_ROUNDS
    max_output_tokens: int = DEFAULT_OUTPUT_TOKENS


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str
    cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)


class RuntimeFailure(RuntimeError):
    """A failed/uncertain turn must not be retried automatically."""


class Runtime(Protocol):
    session_id: str | None

    async def start(self) -> None: ...

    async def turn(self, prompt: str) -> TurnResult: ...

    async def close(self) -> None: ...
