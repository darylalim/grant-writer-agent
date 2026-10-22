"""Making a declined reply say so.

Opus 5.5 and Sonnet 5.5 run safety classifiers that can decline a health or
biomedical request falsely. The API answers that with HTTP 200,
``stop_reason: "refusal"`` and empty or partial content -- no exception, and
nothing in langchain-anthropic names it. Left alone, a declined orchestrator
turn renders as nothing at all, and a declined subagent hands `task` an empty
result (or stale prose from an earlier turn), so the orchestrator carries on
without a section and neither frontend says why.

`RefusalMiddleware` appends one fixed sentence to such a reply, written by
`activity.declined_text`. That is the one thing every reader receives: the
frontends' parser turns it into a DECLINED event, `deepagents` carries it from
a subagent up into the `task` result, and the orchestrator model reads it and
knows a section is missing because it was declined rather than forgotten. The
reply's `response_metadata` is left untouched, so `stop_reason` and
`stop_details` still say exactly what the API said.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from grant_writer.activity import declined_text


class RefusalMiddleware(AgentMiddleware):
    """Append the declined sentence to any reply the API refused.

    ``agent`` names the graph or subagent it is installed on, so the sentence
    can say *which* agent was declined -- from the orchestrator's stream, a
    subagent's result carries no other record of who produced it.
    """

    def __init__(self, *, agent: str) -> None:
        super().__init__()
        self.agent = agent

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return self._mark(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return self._mark(await handler(request))

    def _mark(self, response: ModelResponse) -> ModelResponse:
        response.result = [self._marked(m) for m in response.result]
        return response

    def _marked(self, message: Any) -> Any:
        if not isinstance(message, AIMessage):
            return message
        if message.response_metadata.get("stop_reason") != "refusal":
            return message
        details = message.response_metadata.get("stop_details") or {}
        sentence = declined_text(
            self.agent, details.get("category"), details.get("explanation")
        )
        # Appended, never replacing: partial content is what the model had
        # written before it was stopped, and the sentence must come last for
        # the parser, which reads it to the end of the text.
        content = message.content
        if isinstance(content, str):
            content = f"{content.rstrip()}\n\n{sentence}" if content else sentence
        else:
            content = [*content, {"type": "text", "text": sentence}]
        return message.model_copy(update={"content": content})
