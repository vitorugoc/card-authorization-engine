"""HTTP layer: routing, validation (pydantic), status mapping. No business logic."""

import hmac
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from psycopg import OperationalError

from authz import logging as authz_logging
from authz import service, signature
from authz.models import (
    AuthorizationRequest,
    AuthorizationResponse,
    AuthorizationView,
    CardPatch,
    CardView,
    ClearingRequest,
    CompanyView,
    ErrorResponse,
    EventResponse,
    ReversalRequest,
)
from authz.service import DomainError


def _required(name: str) -> str:
    """Fail at boot, not on the first swipe: a missing secret must never reach a request."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


WEBHOOK_SECRET = _required("WEBHOOK_SECRET")
OPS_TOKEN = _required("OPS_TOKEN")
_required("DATABASE_URL")

authz_logging.configure()
app = FastAPI(title="Card Authorization Engine")


@app.middleware("http")
async def process_time(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{(time.perf_counter() - started) * 1000:.1f}ms"
    return response


@app.exception_handler(DomainError)
def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.code})


@app.exception_handler(OperationalError)
def db_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
    # Reversal/clearing on timeout: 503 so the processor retries (idempotent by event_id).
    return JSONResponse(status_code=503, content={"error": "system_error"})


@app.get("/health", responses={503: {"model": ErrorResponse}})
def health() -> dict[str, str]:
    """Readiness: ok only if a pooled connection can reach Postgres (else 503 via handler)."""
    service.ping()
    return {"status": "ok"}


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorResponse} for status in statuses}


# --- processor-facing (signed webhooks) ----------------------------------------------------


async def verified_webhook(request: Request, x_signature: Annotated[str, Header()] = "") -> None:
    """Runs before body validation: an unsigned caller learns nothing about our schema."""
    body = await request.body()  # raw bytes: the MAC is over exactly what was sent
    if not signature.verify(WEBHOOK_SECRET, x_signature, body, now=time.time()):
        logging.getLogger("authz").warning(
            "webhook.invalid_signature", extra={"fields": {"path": request.url.path}}
        )
        raise DomainError(401, "invalid_signature")


processor = APIRouter(dependencies=[Depends(verified_webhook)], responses=errors(401))


@processor.post("/v1/authorizations", responses=errors(409))
def authorize(req: AuthorizationRequest) -> AuthorizationResponse:
    """Approve or decline. A decline is a business outcome: HTTP 200 (ADR 0005)."""
    return AuthorizationResponse.model_validate(service.authorize(req))


@processor.post(
    "/v1/authorizations/{network_transaction_id}/reversals", responses=errors(404, 409, 503)
)
def reverse(network_transaction_id: str, req: ReversalRequest) -> EventResponse:
    return EventResponse.model_validate(service.reverse(network_transaction_id, req))


@processor.post(
    "/v1/authorizations/{network_transaction_id}/clearings", responses=errors(404, 409, 503)
)
def clear(network_transaction_id: str, req: ClearingRequest) -> EventResponse:
    return EventResponse.model_validate(service.clear(network_transaction_id, req))


app.include_router(processor)


# --- ops (card program admins) ------------------------------------------------------------


def ops_token(authorization: Annotated[str, Header()] = "") -> None:
    # ponytail: one shared bearer token; per-user identity + audit trail (SSO) in production.
    expected = f"Bearer {OPS_TOKEN}".encode()
    if not hmac.compare_digest(authorization.encode(), expected):  # constant time
        raise DomainError(401, "unauthorized")


ops = APIRouter(dependencies=[Depends(ops_token)], responses=errors(401))


@ops.get("/v1/companies/{company_id}", responses=errors(404))
def get_company(company_id: str) -> CompanyView:
    return CompanyView.model_validate(service.get_company(company_id))


@ops.get("/v1/cards/{card_id}", responses=errors(404))
def get_card(card_id: str) -> CardView:
    return CardView.model_validate(service.get_card(card_id))


@ops.patch("/v1/cards/{card_id}", responses=errors(404))
def update_card(card_id: str, patch: CardPatch) -> CardView:
    return CardView.model_validate(service.update_card(card_id, patch))


@ops.get("/v1/cards/{card_id}/authorizations")
def list_card_authorizations(card_id: str) -> list[AuthorizationView]:
    return [AuthorizationView.model_validate(r) for r in service.list_card_authorizations(card_id)]


app.include_router(ops)
