"""HTTP layer: routing, validation (pydantic), status mapping. No business logic."""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from psycopg import OperationalError

from authz import logging as authz_logging
from authz import service
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- processor-facing ----------------------------------------------------------------------


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorResponse} for status in statuses}


@app.post("/v1/authorizations", responses=errors(409))
def authorize(req: AuthorizationRequest) -> AuthorizationResponse:
    """Approve or decline. A decline is a business outcome: HTTP 200 (ADR 0005)."""
    return AuthorizationResponse.model_validate(service.authorize(req))


@app.post("/v1/authorizations/{network_transaction_id}/reversals", responses=errors(404, 409, 503))
def reverse(network_transaction_id: str, req: ReversalRequest) -> EventResponse:
    return EventResponse.model_validate(service.reverse(network_transaction_id, req))


@app.post("/v1/authorizations/{network_transaction_id}/clearings", responses=errors(404, 409, 503))
def clear(network_transaction_id: str, req: ClearingRequest) -> EventResponse:
    return EventResponse.model_validate(service.clear(network_transaction_id, req))


# --- ops -----------------------------------------------------------------------------------


@app.get("/v1/companies/{company_id}", responses=errors(404))
def get_company(company_id: str) -> CompanyView:
    return CompanyView.model_validate(service.get_company(company_id))


@app.get("/v1/cards/{card_id}", responses=errors(404))
def get_card(card_id: str) -> CardView:
    return CardView.model_validate(service.get_card(card_id))


@app.patch("/v1/cards/{card_id}", responses=errors(404))
def update_card(card_id: str, patch: CardPatch) -> CardView:
    # ponytail: unauthenticated for the local demo; first thing to lock down (WRITEUP).
    return CardView.model_validate(service.update_card(card_id, patch))


@app.get("/v1/cards/{card_id}/authorizations")
def list_card_authorizations(card_id: str) -> list[AuthorizationView]:
    return [AuthorizationView.model_validate(r) for r in service.list_card_authorizations(card_id)]
