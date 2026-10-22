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

Each refusal is also recorded as a child run named ``declined`` (D2.7), tagged
``declined`` and ``declined:<category>`` with the category and agent in its
metadata, so refused replies can be filtered and counted in LangSmith. A child
run rather than the turn's own metadata because that is fixed before the turn
starts (`config.trace_config`) and the refusal is only known after the reply;
the child inherits the turn's config, so it sits in the turn's trace beside
the model call, carrying the turn's ``frontend``, ``command`` and ``app_id``.
Nothing new reaches the checkpoint: it copies only the graph's own config
metadata, and both values are already in the thread as the sentence.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda

from grant_writer.activity import declined_category, declined_text

#: An identity runnable: invoking it is what opens the ``declined`` run.
_RECORD = RunnableLambda(lambda details: details, name="declined")


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
        response = handler(request)
        for category in self._mark(response):
            _RECORD.invoke({"category": category}, config=self._trace(category))
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        for category in self._mark(response):
            await _RECORD.ainvoke({"category": category}, config=self._trace(category))
        return response

    def _trace(self, category: str) -> RunnableConfig:
        return {
            "tags": ["declined", f"declined:{category}"],
            "metadata": {
                "stop_reason": "refusal",
                "refusal_category": category,
                "declined_agent": self.agent,
            },
        }

    def _mark(self, response: ModelResponse) -> list[str]:
        """Mark every refused reply in place; return their categories."""
        categories: list[str] = []
        marked = []
        for message in response.result:
            if (
                isinstance(message, AIMessage)
                and message.response_metadata.get("stop_reason") == "refusal"
            ):
                details = message.response_metadata.get("stop_details") or {}
                categories.append(declined_category(details.get("category")))
                message = self._marked(message, details)
            marked.append(message)
        response.result = marked
        return categories

    def _marked(self, message: AIMessage, details: dict[str, Any]) -> AIMessage:
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
