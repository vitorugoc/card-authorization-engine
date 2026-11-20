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
