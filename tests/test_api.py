import json
import time

from authz.signature import sign
from conftest import auth_request, signed_post
from fastapi.testclient import TestClient

from authz.api import app

client = TestClient(app)


def payload(**overrides: object) -> dict[str, object]:
    return {**auth_request().model_dump(), **overrides}


def test_api_approves_with_200(card: str) -> None:
    res = signed_post(client, "/v1/authorizations", payload())
    assert res.status_code == 200
    assert res.json()["decision"] == "approved"
    assert "X-Process-Time" in res.headers


def test_api_rejects_float_amount_422(card: str) -> None:
    assert signed_post(client, "/v1/authorizations", payload(amount=120.5)).status_code == 422


def test_api_rejects_bad_mcc_and_currency_422(card: str) -> None:
    assert signed_post(client, "/v1/authorizations", payload(mcc="58a2")).status_code == 422
    assert signed_post(client, "/v1/authorizations", payload(currency="mxn")).status_code == 422


def test_api_ignores_unknown_processor_fields(card: str) -> None:
    res = signed_post(client, "/v1/authorizations", payload(new_processor_field="x"))
    assert res.status_code == 200


def test_api_idempotency_mismatch_is_409(card: str) -> None:
    signed_post(client, "/v1/authorizations", payload())
    res = signed_post(client, "/v1/authorizations", payload(amount=99_000))
    assert res.status_code == 409
    assert res.json() == {"error": "idempotency_mismatch"}


def test_api_reversal_and_clearing_flow(card: str) -> None:
    signed_post(client, "/v1/authorizations", payload())
    rev = signed_post(
        client, "/v1/authorizations/ntx_1/reversals", {"event_id": "e1", "amount": 2000}
    )
    assert rev.status_code == 200
    assert rev.json()["held_amount"] == 10_000
    clr = signed_post(
        client, "/v1/authorizations/ntx_1/clearings", {"event_id": "e2", "amount": 9000}
    )
    assert clr.status_code == 200
    assert clr.json()["status"] == "cleared"


def test_api_event_errors(card: str) -> None:
    res = signed_post(client, "/v1/authorizations/ntx_x/reversals", {"event_id": "e1"})
    assert (res.status_code, res.json()) == (404, {"error": "authorization_not_found"})
    res = signed_post(client, "/v1/authorizations/ntx_x/clearings", {"event_id": "e1", "amount": 0})
    assert res.status_code == 422


def test_openapi_documents_typed_response_contracts() -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    decision = schemas["AuthorizationResponse"]["properties"]["decision"]
    assert decision["enum"] == ["approved", "declined"]
    for name in ("EventResponse", "CompanyView", "CardView", "AuthorizationView", "ErrorResponse"):
        assert name in schemas, name

    auth_responses = spec["paths"]["/v1/authorizations"]["post"]["responses"]
    assert auth_responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AuthorizationResponse"
    )
    assert auth_responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )


def test_unsigned_processor_request_is_401(card: str) -> None:
    res = client.post("/v1/authorizations", json=payload())
    assert (res.status_code, res.json()) == (401, {"error": "invalid_signature"})


def test_tampered_body_after_signing_is_401(card: str) -> None:
    body = json.dumps(payload()).encode()
    header = sign("whsec_local_demo_only", body, int(time.time()))
    tampered = body.replace(b"12000", b"99000")
    res = client.post(
        "/v1/authorizations",
        content=tampered,
        headers={"Content-Type": "application/json", "X-Signature": header},
    )
    assert res.status_code == 401


def test_stale_signature_is_401(card: str) -> None:
    body = json.dumps(payload()).encode()
    header = sign("whsec_local_demo_only", body, int(time.time()) - 3600)
    res = client.post(
        "/v1/authorizations",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": header},
    )
    assert res.status_code == 401


def test_signature_also_guards_reversals_and_clearings(card: str) -> None:
    for path in ("/v1/authorizations/ntx_1/reversals", "/v1/authorizations/ntx_1/clearings"):
        assert client.post(path, json={"event_id": "e1", "amount": 1}).status_code == 401
