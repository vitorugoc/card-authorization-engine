"""Webhook signature: HMAC-SHA256 over "<t>.<raw body>", timestamp inside the MAC."""

from authz.signature import sign, verify

SECRET = "whsec_test"
BODY = b'{"amount":12000}'
NOW = 1_800_000_000


def test_valid_signature_verifies() -> None:
    assert verify(SECRET, sign(SECRET, BODY, NOW), BODY, now=NOW)


def test_tampered_body_fails() -> None:
    assert not verify(SECRET, sign(SECRET, BODY, NOW), b'{"amount":99000}', now=NOW)


def test_wrong_secret_fails() -> None:
    assert not verify(SECRET, sign("whsec_other", BODY, NOW), BODY, now=NOW)


def test_timestamp_outside_tolerance_fails_both_ways() -> None:
    assert verify(SECRET, sign(SECRET, BODY, NOW - 300), BODY, now=NOW)
    assert not verify(SECRET, sign(SECRET, BODY, NOW - 301), BODY, now=NOW)  # replayed later
    assert not verify(SECRET, sign(SECRET, BODY, NOW + 301), BODY, now=NOW)  # clock games


def test_timestamp_is_covered_by_the_mac() -> None:
    forged = sign(SECRET, BODY, NOW - 3600).replace(f"t={NOW - 3600}", f"t={NOW}")
    assert not verify(SECRET, forged, BODY, now=NOW)


def test_malformed_headers_fail() -> None:
    for header in ("", "garbage", "t=abc,v1=00", f"t={NOW}", "v1=00"):
        assert not verify(SECRET, header, BODY, now=NOW), header
