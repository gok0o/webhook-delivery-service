"""
worker.py — Background delivery worker.

This is the heart of the service. An asyncio task runs in the same process
as the FastAPI server. Every POLL_INTERVAL seconds it:
  1. Queries the DB for pending/due-for-retry attempts
  2. Fires HTTP requests to each subscriber URL (concurrently, via asyncio)
  3. Records the result (success or failure) and schedules a retry if needed

Retry policy (exponential backoff):
  Attempt 1: immediate
  Attempt 2: 30s delay
  Attempt 3: 5 min delay
  Attempt 4: 30 min delay
  Attempt 5: 2 hour delay
  After attempt 5: mark as 'failed', stop retrying

When NOT to retry (per the spec hints):
  - 2xx: delivered, done
  - 4xx except 408/429: subscriber rejected us intentionally, don't retry
  - 5xx, network errors, 408, 429: retry with backoff

What happens if the process crashes mid-delivery?
  The attempt stays in 'pending' or 'retrying' status in the DB. On restart,
  the worker picks it up again on the next poll. This gives us "at least once"
  delivery semantics — a subscriber may get the same event twice if we crash
  after sending but before recording success. That's the documented tradeoff.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app import database as db
from app.signing import sign_payload

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5       # seconds between DB polls
REQUEST_TIMEOUT = 10    # seconds to wait for subscriber response
MAX_ATTEMPTS = 5        # give up after this many tries

# Delay before each retry attempt (index = attempt number, 1-based)
# attempt 1 = immediate (no delay needed, it's the first try)
BACKOFF_DELAYS = {
    2: timedelta(seconds=30),
    3: timedelta(minutes=5),
    4: timedelta(minutes=30),
    5: timedelta(hours=2),
}

# HTTP status codes that should NOT trigger a retry
# 4xx usually means the subscriber rejected us (bad payload, unauthorized, etc.)
# Exceptions: 408 Request Timeout, 429 Too Many Requests — these are transient
NO_RETRY_STATUS_CODES = set(range(400, 500)) - {408, 429}


async def deliver_one(attempt: dict, client: httpx.AsyncClient) -> None:
    """
    Attempts to deliver a single webhook. Updates DB with the result.
    'attempt' is a dict from delivery_attempts JOIN subscriptions.
    """
    attempt_id = attempt["id"]
    event_id = attempt["event_id"]
    target_url = attempt["target_url"]
    secret = attempt["secret"]
    attempt_num = attempt["attempt_number"]

    # Load the event payload
    event = db.get_event(event_id)
    if not event:
        logger.error(f"Event {event_id} not found for attempt {attempt_id}")
        db.update_delivery_attempt(attempt_id, status="failed", error_message="Event not found")
        return

    # Build the request body
    body_dict = {
        "event_id": event_id,
        "event_type": event["event_type"],
        "payload": event["payload"],
        "attempt_number": attempt_num,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }
    body_bytes = json.dumps(body_dict).encode("utf-8")

    # Build headers
    headers = {"Content-Type": "application/json", "User-Agent": "WebhookService/1.0"}
    if secret:
        headers["X-Webhook-Signature"] = sign_payload(secret, body_bytes)

    # Attempt delivery
    try:
        response = await client.post(target_url, content=body_bytes, headers=headers)
        status_code = response.status_code
        resp_body = response.text[:500]  # store only first 500 chars

        if 200 <= status_code < 300:
            # Success!
            logger.info(f"Delivered {event_id} to {target_url} (attempt {attempt_num})")
            db.update_delivery_attempt(
                attempt_id,
                status="success",
                response_code=status_code,
                response_body=resp_body,
                next_retry_at=None,
            )

        elif status_code in NO_RETRY_STATUS_CODES:
            # Permanent failure — subscriber rejected us
            logger.warning(f"Permanent failure {status_code} for {event_id} → {target_url}")
            db.update_delivery_attempt(
                attempt_id,
                status="failed",
                response_code=status_code,
                response_body=resp_body,
                error_message=f"Subscriber returned {status_code}, not retrying",
            )

        else:
            # Transient failure — schedule a retry if we haven't exceeded max attempts
            _schedule_retry(attempt_id, attempt_num, f"HTTP {status_code}", status_code, resp_body)

    except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.warning(f"Network error delivering {event_id} to {target_url}: {error_msg}")
        _schedule_retry(attempt_id, attempt_num, error_msg, None, None)


def _schedule_retry(
    attempt_id: str,
    current_attempt_num: int,
    error_message: str,
    response_code,
    response_body,
) -> None:
    """
    If we have attempts left, schedule the next retry.
    Otherwise, mark as permanently failed.
    """
    next_attempt = current_attempt_num + 1

    if next_attempt > MAX_ATTEMPTS:
        db.update_delivery_attempt(
            attempt_id,
            status="failed",
            response_code=response_code,
            response_body=response_body,
            error_message=f"Gave up after {MAX_ATTEMPTS} attempts. Last error: {error_message}",
        )
        return

    delay = BACKOFF_DELAYS.get(next_attempt, timedelta(hours=2))
    next_retry = (datetime.now(timezone.utc) + delay).isoformat()

    db.update_delivery_attempt(
        attempt_id,
        status="retrying",
        attempt_number=next_attempt,
        next_retry_at=next_retry,
        response_code=response_code,
        response_body=response_body,
        error_message=error_message,
    )
    logger.info(f"Scheduled retry {next_attempt} for attempt {attempt_id} at {next_retry}")


async def run_worker() -> None:
    """
    Main worker loop. Runs forever until the task is cancelled.
    Uses a single httpx.AsyncClient for connection pooling across all deliveries
    in a single poll cycle.
    """
    logger.info("Delivery worker started")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        while True:
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                pending = db.get_pending_attempts(now_iso)

                if pending:
                    logger.info(f"Processing {len(pending)} pending delivery attempts")
                    # Deliver all pending attempts concurrently
                    tasks = [deliver_one(attempt, client) for attempt in pending]
                    await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as exc:
                # Worker must never crash — log and keep going
                logger.exception(f"Worker poll error: {exc}")

            await asyncio.sleep(POLL_INTERVAL)
