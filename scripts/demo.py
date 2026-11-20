"""Narrated end-to-end demo against the running API. `--pause` waits for Enter between steps."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

API = os.environ.get("API_URL", "http://localhost:8000")
PAUSE = "--pause" in sys.argv
GREEN, RED, YELLOW, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"

client = httpx.Client(base_url=API, timeout=5, limits=httpx.Limits(max_connections=20))


def money(minor: int | None) -> str:
    return "—" if minor is None else f"{minor / 100:,.2f} MXN"


def step(n: int, title: str) -> None:
    if PAUSE and n > 1:
        input(f"\n{YELLOW}[enter]{RESET}")
    print(f"\n{BOLD}── {n}. {title}{RESET}")


def state() -> None:
    company = client.get("/v1/companies/co_demo").json()
    card = client.get("/v1/cards/card_demo").json()
    print(
        f"   company: available {money(company['available'])}"
        f" (held {money(company['held'])}, posted {money(company['posted'])})"
        f" · card month spend {money(card['monthly_spend'])} / {money(card['monthly_limit'])}"
    )


def auth(
    ntid: str,
    amount: int,
    mcc: str = "5812",
    merchant: str = "Taqueria El Paso",
    merchant_amount: int = 700,
    merchant_currency: str = "USD",
    show: bool = True,
) -> Any:
    res = client.post("/v1/authorizations", json={
        "network_transaction_id": ntid, "card_id": "card_demo",
        "amount": amount, "currency": "MXN",
        "merchant_amount": merchant_amount, "merchant_currency": merchant_currency,
        "mcc": mcc, "merchant_name": merchant, "merchant_country": "US",
    })  # fmt: skip
    if show:
        body = res.json()
        if res.status_code != 200:
            print(f"   {ntid}: HTTP {res.status_code} {body}")
        elif body["decision"] == "approved":
            print(f"   {ntid} {money(amount)} at {merchant}: {GREEN}APPROVED{RESET}"
                  f"  ({res.headers['x-process-time']})")  # fmt: skip
        else:
            print(f"   {ntid} {money(amount)} at {merchant}: {RED}DECLINED{RESET}"
                  f" {body['decline_code']}  reasons={body['reasons']}")  # fmt: skip
    return res


step(1, "Cross-border lunch: merchant charges 7.00 USD, network bills 120.00 MXN")
auth("ntx_lunch", 12_000)
state()

step(2, "Casino, 600.00: blocked MCC AND over per-transaction limit — every reason returned")
auth("ntx_casino", 60_000, mcc="7995", merchant="Lucky Casino", merchant_amount=60_000,
     merchant_currency="MXN")  # fmt: skip
state()

step(3, "Processor retries the lunch auth (network timeout) — then a tampered retry")
first = auth("ntx_lunch", 12_000, show=False).json()
again = auth("ntx_lunch", 12_000, show=False).json()
print(f"   same ntid, same payload: identical response = {again == first}, no new hold")
auth("ntx_lunch", 99_000)
state()

step(4, "Burst: 20 concurrent 100.00 auths, 880.00 left on the card this month")
with ThreadPoolExecutor(max_workers=20) as pool:
    results = list(pool.map(lambda i: auth(f"ntx_burst_{i}", 10_000, show=False).json(), range(20)))
approved = sum(r["decision"] == "approved" for r in results)
codes = sorted({r["decline_code"] for r in results if r["decline_code"]})
print(f"   {GREEN}{approved} approved{RESET}, {RED}{20 - approved} declined{RESET} {codes}")
card = client.get("/v1/cards/card_demo").json()
ok = card["monthly_spend"] <= card["monthly_limit"]
print(f"   invariant month spend <= limit: {GREEN + 'OK' if ok else RED + 'VIOLATED'}{RESET}")
state()

step(5, "Partial reversal: restaurant releases 20.00 of the lunch hold")
res = client.post("/v1/authorizations/ntx_lunch/reversals", json={"event_id": "evt_rev_1",
                                                                  "amount": 2_000}).json()  # fmt: skip
print(f"   status {res['status']}, hold now {money(res['held_amount'])}")
state()

step(6, "Clearing arrives at 138.00 (tip added): posted in full, rest of hold released")
res = client.post("/v1/authorizations/ntx_lunch/clearings", json={"event_id": "evt_clr_1",
                                                                  "amount": 13_800}).json()  # fmt: skip
print(f"   status {res['status']}, posted {money(res['cleared_amount'])},"
      f" hold {money(res['held_amount'])}")  # fmt: skip
state()

step(7, "Admin freezes the card (lost card) — next swipe declines")
client.patch("/v1/cards/card_demo", json={"status": "frozen"})
auth("ntx_after_freeze", 5_000)

print(
    f"\n{BOLD}Done.{RESET} Proof under the hood: `make test` (concurrency, idempotency, lifecycle)."
)
