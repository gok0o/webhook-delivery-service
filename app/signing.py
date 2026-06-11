"""
signing.py — Payload signing with HMAC-SHA256.

When a subscription has a secret, we sign the raw JSON body and attach
the signature as a header: X-Webhook-Signature: sha256=<hex_digest>

Subscribers verify it like this:
    expected = hmac.new(secret.encode(), body, sha256).hexdigest()
    assert "sha256=" + expected == request.headers["X-Webhook-Signature"]

This lets subscribers confirm the payload really came from us and wasn't
tampered with in transit.
"""

import hashlib
import hmac


def sign_payload(secret: str, body: bytes) -> str:
    """
    Returns a header value like: sha256=abc123...
    'body' must be the raw bytes that will be sent (JSON-encoded).
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """
    Returns True if the given signature header matches what we'd compute.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature_header)
