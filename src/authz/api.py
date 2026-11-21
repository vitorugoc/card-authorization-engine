"""HTTP layer: routing, validation (pydantic), status mapping. No business logic."""

import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from psycopg import OperationalError

from authz import db, service, signature
from authz import logging as authz_logging
from authz.config import Settings, get_settings
from authz.domain import Conflict, DomainError, NotFound
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Fail at boot, not on the first swipe: missing config must never reach a request.
    settings = get_settings()
    authz_logging.configure()
    db.open_pool(settings.database_url)
    yield
    db.close_pool()


app = FastAPI(title="Card Authorization Engine", lifespan=lifespan)
Config = Annotated[Settings, Depends(get_settings)]


@app.middleware("http")
async def process_time(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{(time.perf_counter() - started) * 1000:.1f}ms"
    return response


class Unauthorized(Exception):
    """Transport-level: the caller could not prove who it is. Not a business rule."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The only place that knows how business errors map to HTTP.
_STATUS: dict[type[DomainError], int] = {NotFound: 404, Conflict: 409}


@app.exception_handler(DomainError)
def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS[type(exc)], content={"error": exc.code})


@app.exception_handler(Unauthorized)
def unauthorized(_: Request, exc: Unauthorized) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": exc.code})


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


async def verified_webhook(
    request: Request, settings: Config, x_signature: Annotated[str, Header()] = ""
) -> None:
    """Runs before body validation: an unsigned caller learns nothing about our schema."""
    body = await request.body()  # raw bytes: the MAC is over exactly what was sent
    if not signature.verify(settings.webhook_secret, x_signature, body, now=time.time()):
        logging.getLogger("authz").warning(
            "webhook.invalid_signature", extra={"fields": {"path": request.url.path}}
        )
        raise Unauthorized("invalid_signature")


processor = APIRouter(dependencies=[Depends(verified_webhook)], responses=errors(401))


@processor.post("/v1/authorizations", responses=errors(409))
def authorize(req: AuthorizationRequest) -> AuthorizationResponse:
    """Approve or decline. A decline is a business outcome: HTTP 200 (ADR 0005)."""
    return service.authorize(req)


@processor.post(
    "/v1/authorizations/{network_transaction_id}/reversals", responses=errors(404, 409, 503)
)
def reverse(network_transaction_id: str, req: ReversalRequest) -> EventResponse:
    return service.reverse(network_transaction_id, req)


@processor.post(
    "/v1/authorizations/{network_transaction_id}/clearings", responses=errors(404, 409, 503)
)
def clear(network_transaction_id: str, req: ClearingRequest) -> EventResponse:
    return service.clear(network_transaction_id, req)


app.include_router(processor)


# --- ops (card program admins) ------------------------------------------------------------


def ops_token(settings: Config, authorization: Annotated[str, Header()] = "") -> None:
    # ponytail: one shared bearer token; per-user identity + audit trail (SSO) in production.
    expected = f"Bearer {settings.ops_token}".encode()
    if not hmac.compare_digest(authorization.encode(), expected):  # constant time
        raise Unauthorized("unauthorized")


ops = APIRouter(dependencies=[Depends(ops_token)], responses=errors(401))


@ops.get("/v1/companies/{company_id}", responses=errors(404))
def get_company(company_id: str) -> CompanyView:
    return service.get_company(company_id)


@ops.get("/v1/cards/{card_id}", responses=errors(404))
def get_card(card_id: str) -> CardView:
    return service.get_card(card_id)


@ops.patch("/v1/cards/{card_id}", responses=errors(404))
def update_card(card_id: str, patch: CardPatch) -> CardView:
    return service.update_card(card_id, patch)


@ops.get("/v1/cards/{card_id}/authorizations")
def list_card_authorizations(card_id: str) -> list[AuthorizationView]:
    return service.list_card_authorizations(card_id)


app.include_router(ops)
