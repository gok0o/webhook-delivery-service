"""
main.py — FastAPI application entry point.

Registers all API routes and the web dashboard, starts the background
delivery worker on startup, and handles graceful shutdown.

Authentication: A single hardcoded admin key checked via the X-API-Key header.
Change ADMIN_KEY via the environment variable WEBHOOK_ADMIN_KEY.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Header, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import database as db
from app.models import EventIngest, SubscriptionCreate
from app.worker import run_worker

# ── Logging setup ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
ADMIN_KEY = os.getenv("WEBHOOK_ADMIN_KEY", "secret-admin-key")
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Lifespan: startup / shutdown ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI's lifespan context manager runs code before the server
    starts accepting requests (startup) and after it shuts down (shutdown).

    We use it to:
      1. Initialise the database (create tables if needed)
      2. Start the background delivery worker as an asyncio task
    """
    db.init_db()
    logger.info("Database initialised")

    worker_task = asyncio.create_task(run_worker())
    logger.info("Worker task started")

    yield  # ← server is running and handling requests here

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logger.info("Worker task stopped")


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Webhook Delivery Service",
    description="Ingest events, fan them out to subscribers, retry on failure.",
    lifespan=lifespan,
)


# ── Auth dependency ───────────────────────────────────────────────────────────
def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """
    FastAPI dependency — injects into route functions automatically.
    Raises 401 if the API key header is missing or wrong.
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

# ── Subscriptions ─────────────────────────────────────────────────────────────

@app.post("/api/subscriptions", dependencies=[Depends(require_api_key)], status_code=201)
def create_subscription(body: SubscriptionCreate):
    """
    Register a new subscription.
    The caller specifies a target URL, an optional event filter, and an
    optional shared secret for HMAC signature verification.
    """
    sub = db.create_subscription(
        target_url=str(body.target_url),
        event_filter=body.event_filter,
        secret=body.secret,
    )
    return sub


@app.get("/api/subscriptions", dependencies=[Depends(require_api_key)])
def list_subscriptions():
    return db.list_subscriptions()


@app.get("/api/subscriptions/{sub_id}", dependencies=[Depends(require_api_key)])
def get_subscription(sub_id: str):
    sub = db.get_subscription(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@app.delete("/api/subscriptions/{sub_id}", dependencies=[Depends(require_api_key)])
def delete_subscription(sub_id: str):
    ok = db.delete_subscription(sub_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"deleted": True}


# ── Event Ingest ──────────────────────────────────────────────────────────────

@app.post("/api/events", dependencies=[Depends(require_api_key)], status_code=202)
def ingest_event(body: EventIngest):
    """
    Accept an event and fan it out to all matching subscriptions.

    Returns 202 Accepted immediately — delivery happens asynchronously
    in the background worker. The caller gets an event ID they can use
    to check delivery status later.

    Fan-out: we find all active subscriptions matching the event type,
    create one delivery_attempt row per subscription, and let the worker
    handle the actual HTTP calls.
    """
    # Persist the event first
    event = db.create_event(body.event_type, body.payload)

    # Fan out: find matching subscriptions and create delivery attempts
    subscriptions = db.get_matching_subscriptions(body.event_type)
    attempt_ids = []
    for sub in subscriptions:
        attempt = db.create_delivery_attempt(event["id"], sub["id"])
        attempt_ids.append(attempt["id"])

    logger.info(
        f"Ingested event {event['id']} ({body.event_type}) → "
        f"{len(subscriptions)} subscription(s)"
    )

    return {
        "event_id": event["id"],
        "event_type": body.event_type,
        "matched_subscriptions": len(subscriptions),
        "delivery_attempt_ids": attempt_ids,
    }


@app.get("/api/events", dependencies=[Depends(require_api_key)])
def list_events():
    return db.list_events()


@app.get("/api/events/{event_id}", dependencies=[Depends(require_api_key)])
def get_event(event_id: str):
    event = db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@app.get("/api/events/{event_id}/attempts", dependencies=[Depends(require_api_key)])
def get_event_attempts(event_id: str):
    event = db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return db.list_attempts_for_event(event_id)


# ── Manual Retry ───────────────────────────────────────────────────────────────

@app.post("/api/deliveries/{attempt_id}/retry", dependencies=[Depends(require_api_key)])
def manual_retry(attempt_id: str):
    """
    Manually reschedule a failed delivery attempt.
    Resets status to 'pending' and clears next_retry_at so the worker
    picks it up on the very next poll cycle.
    """
    attempt = db.get_delivery_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Delivery attempt not found")
    if attempt["status"] not in ("failed",):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry attempt with status '{attempt['status']}'. Only 'failed' attempts can be retried.",
        )

    db.update_delivery_attempt(
        attempt_id,
        status="pending",
        attempt_number=1,  # reset attempt counter
        next_retry_at=None,
        error_message=None,
    )
    return {"retrying": True, "attempt_id": attempt_id}


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard (web UI)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Main dashboard — shows recent events and subscription list."""
    events = db.list_events(limit=30)
    subscriptions = db.list_subscriptions()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "events": events, "subscriptions": subscriptions},
    )


@app.get("/dashboard/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str):
    """Drill into a single event and all its delivery attempts."""
    event = db.get_event_with_attempts(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return templates.TemplateResponse(
        "event_detail.html",
        {"request": request, "event": event},
    )


@app.get("/dashboard/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request):
    """Full subscription management page."""
    subscriptions = db.list_subscriptions()
    return templates.TemplateResponse(
        "subscriptions.html",
        {"request": request, "subscriptions": subscriptions},
    )


# ── Health check (no auth needed) ────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}
