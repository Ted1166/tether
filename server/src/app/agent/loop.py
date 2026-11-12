import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.llm import LanguageModel, LLMOutputError, ToolCall
from app.agent.prompts.router import SYSTEM_PROMPT, build_user_message
from app.agent.tools import (
    ASK_CLARIFICATION,
    CREATE_INVOICE,
    DECLINE_REQUEST,
    TOOLS,
    TOOLS_BY_NAME,
    AskClarificationInput,
    CreateInvoiceInput,
    DeclineInput,
    to_proposal,
)
from app.lib.time import utc_now
from app.schemas.agent import AgentReply, AgentReplyKind
from app.schemas.records import PendingActionRecord, PendingActionStatus
from app.services.actions import ActionService

logger = logging.getLogger("tether.agent")

MAX_ATTEMPTS = 2


class AgentOutputError(Exception):
    pass


def _utc_today() -> date:
    return utc_now().date()


class Agent:
    def __init__(
        self,
        model: LanguageModel,
        actions: ActionService,
        *,
        default_currency: str = "USD",
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self._model = model
        self._actions = actions
        self._default_currency = default_currency
        self._today = today

    async def handle(self, message: str) -> AgentReply:
        known = await self._actions.known_clients()
        prompt = build_user_message(
            message,
            today=self._today(),
            default_currency=self._default_currency,
            known_clients=known,
        )
        name, arguments = await self._ask(prompt)

        if isinstance(arguments, CreateInvoiceInput):
            try:
                proposal = to_proposal(arguments)
            except ValidationError:
                return AgentReply(
                    kind=AgentReplyKind.CLARIFICATION,
                    message=(
                        "I could not build a valid invoice from that. Could you confirm the "
                        "client's email address and the amounts?"
                    ),
                    missing_fields=("client_email",),
                )
            record = await self._actions.propose_invoice(proposal)
            return AgentReply(
                kind=AgentReplyKind.PROPOSAL, message=_describe(record), action=record
            )

        if isinstance(arguments, AskClarificationInput):
            return AgentReply(
                kind=AgentReplyKind.CLARIFICATION,
                message=arguments.question,
                missing_fields=tuple(arguments.missing_fields),
            )

        assert isinstance(arguments, DeclineInput), name
        return AgentReply(kind=AgentReplyKind.DECLINED, message=arguments.reason)

    async def _ask(self, prompt: str) -> tuple[str, BaseModel]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_problem = ""

        for attempt in range(MAX_ATTEMPTS):
            try:
                call = await self._model.call_tool(
                    system=SYSTEM_PROMPT, messages=messages, tools=TOOLS
                )
            except LLMOutputError as error:
                last_problem = str(error)
                logger.warning("no usable tool call (attempt %s): %s", attempt + 1, last_problem)
                messages.append(
                    {"role": "user", "content": "Reply by calling exactly one of the tools."}
                )
                continue
            parsed, problem = _validate(call)
            if parsed is not None:
                return call.name, parsed

            last_problem = problem
            logger.warning("model output rejected (attempt %s): %s", attempt + 1, problem)
            messages.extend(
                [
                    {"role": "assistant", "content": list(call.content)},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "is_error": True,
                                "content": f"That call was invalid: {problem}. Call a tool again.",
                            }
                        ],
                    },
                ]
            )

        raise AgentOutputError(f"the model kept returning invalid output: {last_problem}")


def _validate(call: ToolCall) -> tuple[BaseModel | None, str]:
    spec = TOOLS_BY_NAME.get(call.name)
    if spec is None:
        return None, f"{call.name!r} is not an available tool"
    try:
        return spec.input_model.model_validate(call.input), ""
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        return None, details
    except (ValueError, LLMOutputError) as error:
        return None, str(error)


def _describe(record: PendingActionRecord) -> str:
    if record.status is PendingActionStatus.BLOCKED:
        return f"I can't do that: {record.reason}. Nothing was created. ({record.summary})"
    if record.status is PendingActionStatus.EXECUTED:
        return f"Done. {record.summary}."
    return f"Ready for your approval: {record.summary}."


__all__ = ["ASK_CLARIFICATION", "CREATE_INVOICE", "DECLINE_REQUEST", "Agent", "AgentOutputError"]
