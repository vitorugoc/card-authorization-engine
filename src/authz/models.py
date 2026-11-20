"""Request models: the trust boundary. Everything past here is validated."""

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
