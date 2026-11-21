"""Ops surface: card reads and live policy changes (PATCH takes the company lock)."""

import os
import subprocess
import sys

import pytest
from conftest import auth_request, client_post
from fastapi.testclient import TestClient
from psycopg import OperationalError

from authz import service
from authz.api import app

client = TestClient(app, headers={"Authorization": f"Bearer {os.environ['OPS_TOKEN']}"})
anonymous = TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/companies/co_test"),
        ("GET", "/v1/cards/card_test"),
        ("GET", "/v1/cards/card_test/authorizations"),
        ("PATCH", "/v1/cards/card_test"),
    ],
)
@pytest.mark.parametrize("token", [None, "Bearer wrong", "ops_local_demo_only"])
def test_ops_routes_require_bearer_token(
    card: str, method: str, path: str, token: str | None
) -> None:
    headers = {"Authorization": token} if token else {}
    res = anonymous.request(method, path, json={"status": "frozen"}, headers=headers)
    assert (res.status_code, res.json()) == (401, {"error": "unauthorized"})
    assert service.get_card(card).status == "active"


def test_app_refuses_to_start_without_required_config() -> None:
    env = {k: v for k, v in os.environ.items() if k != "OPS_TOKEN"}
    boot = (
        "from fastapi.testclient import TestClient; from authz.api import app\n"
        "with TestClient(app): pass"  # runs the lifespan, like uvicorn startup
    )
    res = subprocess.run([sys.executable, "-c", boot], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    assert "missing required config: OPS_TOKEN" in res.stderr


def test_health_checks_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    assert anonymous.get("/health").json() == {"status": "ok"}

    def down() -> None:
        raise OperationalError("connection refused")

    monkeypatch.setattr(service, "ping", down)
    res = anonymous.get("/health")
    assert (res.status_code, res.json()) == (503, {"error": "system_error"})


def test_patch_freeze_then_auth_declines_card_inactive(card: str) -> None:
    res = client.patch(f"/v1/cards/{card}", json={"status": "frozen"})
    assert res.status_code == 200
    assert res.json()["status"] == "frozen"

    assert client_post(client, auth_request())["decline_code"] == "card_inactive"


def test_patch_limits_and_mccs(card: str) -> None:
    res = client.patch(
        f"/v1/cards/{card}", json={"per_txn_limit": 1_000, "blocked_mccs": ["5812", "7995"]}
    )
    assert res.json()["per_txn_limit"] == 1_000
    assert res.json()["blocked_mccs"] == ["5812", "7995"]
    assert client_post(client, auth_request(amount=1_001))["reasons"] == [
        "mcc_blocked",
        "exceeds_per_transaction_limit",
    ]


def test_patch_rejects_unknown_fields_and_bad_values_422(card: str) -> None:
    assert client.patch(f"/v1/cards/{card}", json={"company_id": "x"}).status_code == 422
    assert client.patch(f"/v1/cards/{card}", json={"status": "deleted"}).status_code == 422


def test_patch_unknown_card_404() -> None:
    res = client.patch("/v1/cards/card_nope", json={"status": "frozen"})
    assert (res.status_code, res.json()) == (404, {"error": "card_not_found"})


def test_get_card_includes_monthly_spend(card: str) -> None:
    client_post(client, auth_request(amount=12_000))
    body = client.get(f"/v1/cards/{card}").json()
    assert body["monthly_spend"] == 12_000
    assert body["last4"] == "4242"


def test_list_card_authorizations_newest_first(card: str) -> None:
    client_post(client, auth_request("ntx_a"))
    client_post(client, auth_request("ntx_b", mcc="7995"))

    items = client.get(f"/v1/cards/{card}/authorizations").json()

    assert [i["network_transaction_id"] for i in items] == ["ntx_b", "ntx_a"]
    assert [i["status"] for i in items] == ["declined", "pending"]
