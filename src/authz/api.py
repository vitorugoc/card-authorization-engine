"""HTTP layer: routing, validation (pydantic), status mapping. No business logic."""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from authz import service
from authz.models import AuthorizationRequest
from authz.service import DomainError

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- processor-facing ----------------------------------------------------------------------


@app.post("/v1/authorizations")
def authorize(req: AuthorizationRequest) -> dict[str, Any]:
    """Approve or decline. A decline is a business outcome: HTTP 200 (ADR 0005)."""
    return service.authorize(req)


# --- ops -----------------------------------------------------------------------------------


@app.get("/v1/companies/{company_id}")
def get_company(company_id: str) -> dict[str, Any]:
    return service.get_company(company_id)
