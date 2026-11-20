"""Request models: the trust boundary. Everything past here is validated."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Amount = Annotated[StrictInt, Field(gt=0, le=10**12)]  # minor units; floats/strings rejected
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Mcc = Annotated[str, Field(pattern=r"^[0-9]{4}$")]
Id = Annotated[str, Field(min_length=1, max_length=64)]


class AuthorizationRequest(BaseModel):
    # Processors add fields over time; an unknown field must not break the auth path.
    model_config = ConfigDict(extra="ignore", frozen=True)

    network_transaction_id: Id
    card_id: Id
    amount: Amount  # billing amount, already converted by the network
    currency: Currency
    merchant_amount: Annotated[StrictInt, Field(ge=0, le=10**12)]  # display only
    merchant_currency: Currency
    mcc: Mcc
    merchant_name: Annotated[str, Field(max_length=255)]
    merchant_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]

    def decision_fields(self) -> dict[str, Any]:
        """Fields that change the decision; their hash is the idempotency fingerprint."""
        return self.model_dump(include={"card_id", "amount", "currency", "mcc", "merchant_country"})


class ReversalRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: Id
    amount: Amount | None = None  # None = release the whole remaining hold


class ClearingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: Id
    amount: Amount  # may differ from the authorized amount (tips, fuel, FX movement)


class CardPatch(BaseModel):
    # Ops input: unknown fields are a client bug, not an evolving processor contract.
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["active", "frozen"] | None = None
    per_txn_limit: Amount | None = None
    monthly_limit: Amount | None = None
    blocked_mccs: list[Mcc] | None = Field(default=None, max_length=100)


# --- responses: the outbound contract (rendered in OpenAPI) --------------------------------

AuthStatus = Literal["declined", "pending", "reversed", "cleared"]


class AuthorizationResponse(BaseModel):
    authorization_id: str | None  # None when nothing was persisted (card_not_found, system_error)
    network_transaction_id: str
    decision: Literal["approved", "declined"]
    decline_code: str | None
    reasons: list[str]
    available_credit_after: int | None


class EventResponse(BaseModel):
    authorization_id: str
    network_transaction_id: str
    event_id: str
    type: Literal["reversal", "clearing"]
    amount: int
    status: AuthStatus
    held_amount: int
    cleared_amount: int
    available_credit_after: int


class CompanyView(BaseModel):
    id: str
    name: str
    currency: str
    credit_limit: int
    held: int
    posted: int
    available: int


class CardView(BaseModel):
    id: str
    company_id: str
    holder_name: str
    last4: str
    status: Literal["active", "frozen"]
    per_txn_limit: int
    monthly_limit: int
    blocked_mccs: list[str]
    monthly_spend: int


class AuthorizationView(BaseModel):
    id: str
    network_transaction_id: str
    amount: int
    currency: str
    merchant_amount: int
    merchant_currency: str
    mcc: str
    merchant_name: str
    merchant_country: str
    status: AuthStatus
    decline_code: str | None
    reasons: list[str]
    held_amount: int
    cleared_amount: int
    created_at: datetime


class ErrorResponse(BaseModel):
    error: str
