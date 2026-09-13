"""The model seam.

`agent.py` talks to this protocol, not to the Anthropic SDK. Two reasons, and
the second is the one that matters:

1. The agent loop is the most intricate code in the project -- budgets, pruning,
   stop-reason handling, the dead-end detector -- and all of it is testable
   offline against a scripted client. A loop that can only be exercised by
   spending money on a live API is a loop that does not get exercised.
2. It keeps the SDK import in one small module, so what depends on `anthropic`
   is obvious by inspection.

Replay never reaches any of this; `test_replay_has_no_llm.py` asserts that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ModelResponse:
    """The one shape the agent understands, whoever produced it."""

    stop_reason: str
    """`tool_use` | `end_turn` | `max_tokens` | `refusal` | `stop_sequence`.
    Checked BEFORE the content is read, every turn: a refusal or a truncated
    turn is a reason to stop cleanly, not something to parse around."""

    content: list[Any] = field(default_factory=list)
    text: str = ""
    tool_calls: list["ToolCall"] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


class ModelClient(Protocol):
    def send(self, *, system: list[dict], messages: list[dict],
             tools: list[dict]) -> ModelResponse: ...


class AnthropicClient:
    """The real one.

    Adaptive thinking rather than a token budget: discovery is the part of this
    system that genuinely requires reasoning -- working out which of forty
    unlabelled table cells is the field you want -- and the model is better
    placed than we are to decide how much of it each turn needs.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        max_tokens: int = 4096,
        effort: str = "high",
        api_key: str | None = None,
    ) -> None:
        import anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Discovery is the only command that "
                "needs it; replay, the console, and the test suite all run offline."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

    def send(self, *, system: list[dict], messages: list[dict],
             tools: list[dict]) -> ModelResponse:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )

        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if getattr(block, "type", "") == "tool_use"
        ]
        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        )
        return ModelResponse(
            stop_reason=response.stop_reason or "",
            content=[b.model_dump() for b in response.content],
            text=text,
            tool_calls=calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class ScriptedClient:
    """A `ModelClient` that replays a fixed list of responses.

    Used by the tests to drive the agent through paths that are expensive or
    impossible to provoke on a live model: a refusal, a truncated turn, a model
    that keeps clicking the same dead control.


    An entry may be a `ModelResponse` or a zero-argument callable returning one.
    The callable form exists because this application regenerates element ids on
    every render -- the whole point of the fixture -- so a script cannot hard-code
    the node it will click three turns from now. It has to look at the screen, in
    the same way the model does.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.sent: list[dict] = []

    def send(self, *, system: list[dict], messages: list[dict],
             tools: list[dict]) -> ModelResponse:
        self.sent.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._responses:
            return ModelResponse(stop_reason="end_turn", text="(script exhausted)")
        nxt = self._responses.pop(0)
        return nxt() if callable(nxt) else nxt


def tool_turn(*calls: tuple[str, dict], text: str = "") -> ModelResponse:
    """Build a scripted `tool_use` turn."""
    return ModelResponse(
        stop_reason="tool_use",
        text=text,
        tool_calls=[ToolCall(id=f"tu_{i}", name=name, arguments=args)
                    for i, (name, args) in enumerate(calls)],
    )
