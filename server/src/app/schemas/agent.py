from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invoice import InvoiceDraft
from app.schemas.records import PendingActionRecord


class InvoiceProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft: InvoiceDraft
    send: bool = True

    @property
    def total(self) -> Decimal:
        return self.draft.total


class AgentReplyKind(StrEnum):
    PROPOSAL = "proposal"
    CLARIFICATION = "clarification"
    DECLINED = "declined"


class AgentReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: AgentReplyKind
    message: str
    action: PendingActionRecord | None = None
    missing_fields: tuple[str, ...] = Field(default=())


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
