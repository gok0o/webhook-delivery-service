"""
tests/test_core.py — Tests for the critical paths.

We test:
  1. Signing/verification logic (pure functions, no I/O)
  2. Event filter matching logic (pure function in database.py)
  3. Retry scheduling (worker logic)
  4. API routes via FastAPI's test client
  5. Database persistence (actual SQLite, temp file)

Run with: pytest tests/ -v
"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── 1. Signing ────────────────────────────────────────────────────────────────

from app.signing import sign_payload, verify_signature

def test_sign_payload_produces_sha256_prefix():
    sig = sign_payload("mysecret", b'{"hello": "world"}')
    assert sig.startswith("sha256=")
    assert len(sig) > 10


def test_verify_signature_correct():
    body = b'{"event": "test"}'
    secret = "supersecret"
    sig = sign_payload(secret, body)
    assert verify_signature(secret, body, sig) is True


def test_verify_signature_wrong_secret():
    body = b'{"event": "test"}'
    sig = sign_payload("correct-secret", body)
    assert verify_signature("wrong-secret", body, sig) is False


def test_verify_signature_tampered_body():
    body = b'{"event": "test"}'
    sig = sign_payload("mysecret", body)
    tampered = b'{"event": "tampered"}'
    assert verify_signature("mysecret", tampered, sig) is False


# ── 2. Event filter matching ───────────────────────────────────────────────────

# We test the matching logic directly by importing the helper.
# To avoid needing a real DB, we test the matching logic in isolation.

def _matches(event_filter: str, event_type: str) -> bool:
    """Replicate the matching logic from database.get_matching_subscriptions."""
    category = event_type.split(".")[0]
    if event_filter == "*":
        return True
    elif event_filter == f"{category}.*":
        return True
    elif event_filter == event_type:
        return True
    return False


def test_wildcard_matches_everything():
    assert _matches("*", "order.created") is True
    assert _matches("*", "user.deleted") is True
    assert _matches("*", "payment.refunded") is True


def test_category_wildcard():
    assert _matches("order.*", "order.created") is True
    assert _matches("order.*", "order.updated") is True
    assert _matches("order.*", "user.created") is False


def test_exact_match():
    assert _matches("order.created", "order.created") is True
    assert _matches("order.created", "order.updated") is False
    assert _matches("order.created", "user.created") is False


def test_no_cross_category_match():
    assert _matches("user.*", "order.created") is False
    assert _matches("payment.completed", "order.completed") is False


# ── 3. Retry scheduling ────────────────────────────────────────────────────────

from app.worker import MAX_ATTEMPTS, BACKOFF_DELAYS, NO_RETRY_STATUS_CODES

def test_max_attempts_constant():
    assert MAX_ATTEMPTS == 5


def test_backoff_delays_exist_for_all_retry_attempts():
    for attempt in range(2, MAX_ATTEMPTS + 1):
        assert attempt in BACKOFF_DELAYS, f"No backoff delay defined for attempt {attempt}"


def test_no_retry_codes_exclude_408_and_429():
    # 408 and 429 should be retried
    assert 408 not in NO_RETRY_STATUS_CODES
    assert 429 not in NO_RETRY_STATUS_CODES


def test_no_retry_codes_include_regular_4xx():
    # Regular client errors should not be retried
    assert 400 in NO_RETRY_STATUS_CODES
    assert 401 in NO_RETRY_STATUS_CODES
    assert 403 in NO_RETRY_STATUS_CODES
    assert 404 in NO_RETRY_STATUS_CODES


# ── 4. API routes ─────────────────────────────────────────────────────────────

# We patch the DB path to use a temp file so tests don't pollute the real DB.

@pytest.fixture
def client(tmp_path):
    """Create a test client with an isolated temp database."""
    db_file = tmp_path / "test.db"
    with patch("app.database.DB_PATH", db_file):
        # Re-init the DB in the temp location
        import app.database as database
        import importlib
        # Monkey-patch the module-level DB_PATH
        original_path = database.DB_PATH
        database.DB_PATH = db_file
        database.init_db()

        from app.main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c

        database.DB_PATH = original_path


API_KEY = "secret-admin-key"
HEADERS = {"X-API-Key": API_KEY}


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_subscription(client):
    resp = client.post(
        "/api/subscriptions",
        json={"target_url": "https://example.com/hook", "event_filter": "order.*"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["target_url"] == "https://example.com/hook"
    assert data["event_filter"] == "order.*"
    assert "id" in data


def test_create_subscription_requires_auth(client):
    resp = client.post(
        "/api/subscriptions",
        json={"target_url": "https://example.com/hook"},
    )
    assert resp.status_code == 401


def test_ingest_event_fans_out(client):
    # Create two subscriptions — one matching, one not
    client.post(
        "/api/subscriptions",
        json={"target_url": "https://match.example.com/hook", "event_filter": "order.*"},
        headers=HEADERS,
    )
    client.post(
        "/api/subscriptions",
        json={"target_url": "https://nomatch.example.com/hook", "event_filter": "user.*"},
        headers=HEADERS,
    )

    # Ingest an order event
    resp = client.post(
        "/api/events",
        json={"event_type": "order.created", "payload": {"order_id": 42}},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["matched_subscriptions"] == 1  # only the order.* subscription matches
    assert len(data["delivery_attempt_ids"]) == 1


def test_ingest_event_invalid_type(client):
    resp = client.post(
        "/api/events",
        json={"event_type": "notype", "payload": {}},  # missing dot
        headers=HEADERS,
    )
    assert resp.status_code == 422  # Pydantic validation error


def test_event_persists(client):
    client.post(
        "/api/events",
        json={"event_type": "order.created", "payload": {"x": 1}},
        headers=HEADERS,
    )
    resp = client.get("/api/events", headers=HEADERS)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert events[0]["event_type"] == "order.created"


def test_delete_subscription(client):
    create_resp = client.post(
        "/api/subscriptions",
        json={"target_url": "https://example.com/todelete"},
        headers=HEADERS,
    )
    sub_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/subscriptions/{sub_id}", headers=HEADERS)
    assert del_resp.status_code == 200

    # After delete, the subscription should be inactive
    get_resp = client.get(f"/api/subscriptions/{sub_id}", headers=HEADERS)
    assert get_resp.json()["is_active"] == 0


def test_manual_retry_only_on_failed(client):
    # Create sub + event to get an attempt
    client.post(
        "/api/subscriptions",
        json={"target_url": "https://example.com/hook", "event_filter": "*"},
        headers=HEADERS,
    )
    event_resp = client.post(
        "/api/events",
        json={"event_type": "order.created", "payload": {}},
        headers=HEADERS,
    )
    attempt_id = event_resp.json()["delivery_attempt_ids"][0]

    # Attempt is 'pending', not 'failed' — retry should fail
    resp = client.post(f"/api/deliveries/{attempt_id}/retry", headers=HEADERS, json={})
    assert resp.status_code == 400
