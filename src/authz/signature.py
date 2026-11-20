"""Webhook signatures, Stripe-style: `X-Signature: t=<unix seconds>,v1=<hex>` where
v1 = HMAC-SHA256(secret, "<t>." + raw body).

The timestamp is inside the MAC, so a captured request can't be replayed after the window.
"""

import hashlib
import hmac

TOLERANCE_SECONDS = 300


def _mac(secret: str, body: bytes, timestamp: int) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int) -> str:
    return f"t={timestamp},v1={_mac(secret, body, timestamp)}"


def verify(
    secret: str, header: str, body: bytes, now: float, tolerance: int = TOLERANCE_SECONDS
) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp, received = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    return hmac.compare_digest(_mac(secret, body, timestamp), received)  # constant time
