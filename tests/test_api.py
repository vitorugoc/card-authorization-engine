from conftest import auth_request
from fastapi.testclient import TestClient

from authz.api import app

client = TestClient(app)


def payload(**overrides: object) -> dict[str, object]:
    return {**auth_request().model_dump(), **overrides}


def test_api_approves_with_200(card: str) -> None:
    res = client.post("/v1/authorizations", json=payload())
    assert res.status_code == 200
    assert res.json()["decision"] == "approved"
    assert "X-Process-Time" in res.headers


def test_api_rejects_float_amount_422(card: str) -> None:
    assert client.post("/v1/authorizations", json=payload(amount=120.5)).status_code == 422


def test_api_rejects_bad_mcc_and_currency_422(card: str) -> None:
    assert client.post("/v1/authorizations", json=payload(mcc="58a2")).status_code == 422
    assert client.post("/v1/authorizations", json=payload(currency="mxn")).status_code == 422


def test_api_ignores_unknown_processor_fields(card: str) -> None:
    res = client.post("/v1/authorizations", json=payload(new_processor_field="x"))
    assert res.status_code == 200


def test_api_idempotency_mismatch_is_409(card: str) -> None:
    client.post("/v1/authorizations", json=payload())
    res = client.post("/v1/authorizations", json=payload(amount=99_000))
    assert res.status_code == 409
    assert res.json() == {"error": "idempotency_mismatch"}


def test_api_reversal_and_clearing_flow(card: str) -> None:
    client.post("/v1/authorizations", json=payload())
    rev = client.post("/v1/authorizations/ntx_1/reversals", json={"event_id": "e1", "amount": 2000})
    assert rev.status_code == 200
    assert rev.json()["held_amount"] == 10_000
    clr = client.post("/v1/authorizations/ntx_1/clearings", json={"event_id": "e2", "amount": 9000})
    assert clr.status_code == 200
    assert clr.json()["status"] == "cleared"


def test_api_event_errors(card: str) -> None:
    res = client.post("/v1/authorizations/ntx_x/reversals", json={"event_id": "e1"})
    assert (res.status_code, res.json()) == (404, {"error": "authorization_not_found"})
    res = client.post("/v1/authorizations/ntx_x/clearings", json={"event_id": "e1", "amount": 0})
    assert res.status_code == 422
