"""
database.py — SQLite database setup and all data-access functions.

Why SQLite?
  - Zero config, single file, survives restarts (satisfies the persistence requirement)
  - Python's built-in sqlite3 module — no extra dependencies
  - Perfectly fine for a single-process service

We use Python's standard sqlite3 module with WAL mode enabled.
WAL (Write-Ahead Logging) allows concurrent reads while a write is happening,
which is important because the worker and the API server run in the same process.

Table design:
  subscriptions   — who wants what events, at which URL
  events          — every ingested event (the source of truth)
  delivery_attempts — each attempt to deliver an event to a subscription
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("webhook_service.db")


def get_conn() -> sqlite3.Connection:
    """
    Opens a connection to the SQLite database.
    row_factory=sqlite3.Row makes rows behave like dicts (row["column_name"]).
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")    # enforce FK constraints
    return conn


def init_db() -> None:
    """
    Creates all tables if they don't exist yet.
    Safe to call on every startup — CREATE TABLE IF NOT EXISTS is idempotent.
    """
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id          TEXT PRIMARY KEY,
                target_url  TEXT NOT NULL,
                event_filter TEXT NOT NULL DEFAULT '*',
                secret      TEXT,           -- nullable; used for HMAC signing
                created_at  TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS events (
                id          TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                payload     TEXT NOT NULL,  -- stored as JSON string
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delivery_attempts (
                id              TEXT PRIMARY KEY,
                event_id        TEXT NOT NULL REFERENCES events(id),
                subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                status          TEXT NOT NULL DEFAULT 'pending',
                -- status values: pending | success | failed | retrying
                attempt_number  INTEGER NOT NULL DEFAULT 1,
                next_retry_at   TEXT,       -- ISO datetime, NULL if done
                response_code   INTEGER,    -- HTTP status from subscriber
                response_body   TEXT,       -- first 500 chars of subscriber response
                error_message   TEXT,       -- network/timeout error if any
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_attempts_status
                ON delivery_attempts(status, next_retry_at);

            CREATE INDEX IF NOT EXISTS idx_attempts_event
                ON delivery_attempts(event_id);
        """)


# ── Subscriptions ────────────────────────────────────────────────────────────

def create_subscription(target_url: str, event_filter: str, secret: Optional[str]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    sub_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO subscriptions (id, target_url, event_filter, secret, created_at) VALUES (?,?,?,?,?)",
            (sub_id, target_url, event_filter, secret, now),
        )
    return get_subscription(sub_id)


def get_subscription(sub_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    return dict(row) if row else None


def list_subscriptions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_subscription(sub_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE subscriptions SET is_active = 0 WHERE id = ?", (sub_id,))
    return cur.rowcount > 0


# ── Events ───────────────────────────────────────────────────────────────────

def create_event(event_type: str, payload: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    event_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (id, event_type, payload, created_at) VALUES (?,?,?,?)",
            (event_id, event_type, json.dumps(payload), now),
        )
    return get_event(event_id)


def get_event(event_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])  # deserialize back to dict
    return d


def list_events(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        result.append(d)
    return result


# ── Delivery Attempts ─────────────────────────────────────────────────────────

def create_delivery_attempt(event_id: str, subscription_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    attempt_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO delivery_attempts
               (id, event_id, subscription_id, status, attempt_number, created_at, updated_at)
               VALUES (?,?,?,'pending',1,?,?)""",
            (attempt_id, event_id, subscription_id, now, now),
        )
    return get_delivery_attempt(attempt_id)


def get_delivery_attempt(attempt_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM delivery_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    return dict(row) if row else None


def update_delivery_attempt(attempt_id: str, **kwargs) -> None:
    """
    Generic updater. Pass keyword args matching column names.
    Example: update_delivery_attempt(id, status="success", response_code=200)
    """
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [attempt_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE delivery_attempts SET {set_clause} WHERE id = ?", values
        )


def list_attempts_for_event(event_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT da.*, s.target_url, s.event_filter
               FROM delivery_attempts da
               JOIN subscriptions s ON da.subscription_id = s.id
               WHERE da.event_id = ?
               ORDER BY da.created_at""",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_attempts(now_iso: str) -> list[dict]:
    """
    Fetch all attempts that are ready to be delivered right now.
    'pending' ones with no next_retry_at, plus 'retrying' ones whose
    next_retry_at is in the past.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT da.*, s.target_url, s.secret
               FROM delivery_attempts da
               JOIN subscriptions s ON da.subscription_id = s.id
               WHERE s.is_active = 1
                 AND (
                   (da.status = 'pending' AND da.next_retry_at IS NULL)
                   OR
                   (da.status = 'retrying' AND da.next_retry_at <= ?)
                 )""",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_event_with_attempts(event_id: str) -> Optional[dict]:
    """Convenience: event + all its delivery attempts joined."""
    event = get_event(event_id)
    if not event:
        return None
    event["attempts"] = list_attempts_for_event(event_id)
    return event


def get_matching_subscriptions(event_type: str) -> list[dict]:
    """
    Returns all active subscriptions whose filter matches the given event_type.
    Matching rules:
      - "*"         matches everything
      - "order.*"   matches any event starting with "order."
      - "order.created" matches exactly "order.created"
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE is_active = 1"
        ).fetchall()

    category = event_type.split(".")[0]
    matched = []
    for row in rows:
        f = row["event_filter"]
        if f == "*":
            matched.append(dict(row))
        elif f == f"{category}.*":
            matched.append(dict(row))
        elif f == event_type:
            matched.append(dict(row))
    return matched
