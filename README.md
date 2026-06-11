# Webhook Delivery Service

A reliable webhook delivery service built with Python (FastAPI + SQLite).

## What it does

- **Subscribe**: clients register target URLs with optional event filters and HMAC secrets
- **Ingest**: accepts events via REST API, fans them out to matching subscriptions
- **Deliver**: background worker delivers events with retries and exponential backoff
- **Persist**: all events and attempts survive process restarts (SQLite)
- **Dashboard**: web UI to browse events, subscriptions, and delivery attempts

---

## Running it

### Prerequisites

- Python 3.11+

### Install & run

```bash
# Clone / unzip the repo, then:
cd webhook-service

# Create a virtual environment
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the server (database is auto-created on first run)
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/dashboard** for the web UI.

### Run tests

```bash
pytest tests/ -v
```

---

## API Quick Start

All API routes require the header `X-API-Key: secret-admin-key`
(override via `WEBHOOK_ADMIN_KEY` environment variable).

### 1. Create a subscription

```bash
curl -X POST http://localhost:8000/api/subscriptions \
  -H "X-API-Key: secret-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"target_url": "https://webhook.site/your-id", "event_filter": "order.*"}'
```

Event filter options:
- `"*"` — receive all events (default)
- `"order.*"` — receive all events in the `order` category
- `"order.created"` — receive only this exact event type

### 2. Ingest an event

```bash
curl -X POST http://localhost:8000/api/events \
  -H "X-API-Key: secret-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "order.created", "payload": {"order_id": 123, "amount": 99.99}}'
```

Returns 202 immediately. Delivery happens asynchronously.

### 3. Check delivery status

```bash
# List recent events
curl http://localhost:8000/api/events -H "X-API-Key: secret-admin-key"

# Get delivery attempts for an event
curl http://localhost:8000/api/events/{event_id}/attempts -H "X-API-Key: secret-admin-key"
```

Or just use the dashboard at `/dashboard`.

### 4. Manual retry

```bash
curl -X POST http://localhost:8000/api/deliveries/{attempt_id}/retry \
  -H "X-API-Key: secret-admin-key"
```

---

## Architecture

```
app/
├── main.py        FastAPI app — all API routes and dashboard routes
├── database.py    SQLite setup and all data-access functions
├── models.py      Pydantic request validation models
├── worker.py      Background async delivery worker
├── signing.py     HMAC-SHA256 payload signing
└── templates/     Jinja2 HTML templates for the dashboard
```

The delivery worker runs as an `asyncio.Task` in the same process as the API server. It polls the database every 5 seconds for pending delivery attempts and fires them concurrently.

---

## What's working

- ✅ Subscription CRUD with event filter patterns
- ✅ Event ingest with fan-out to matching subscriptions
- ✅ Async delivery worker with retries and exponential backoff
- ✅ HMAC-SHA256 payload signing
- ✅ Persistence — events and attempts survive restart
- ✅ Dashboard — event list, event detail with attempts, subscription manager
- ✅ Manual retry from the dashboard
- ✅ Tests for signing, filter matching, retry logic, and API routes

## What's incomplete / would improve with more time

- **Delivery attempt counter in dashboard summary**: the stats card shows event count but not aggregated delivery success/failure rates
- **Webhook signature verification endpoint**: no `/verify` helper for subscribers to test against
- **Event payload search/filter**: the dashboard shows all events but has no search
- **Better error UI**: API validation errors show raw JSON — could be friendlier
- **Rate limiting**: no protection against a subscriber being hammered during retry storms (the backoff helps but doesn't cap total concurrency per subscriber)
- **Dead letter queue view**: failed-permanently events deserve their own dashboard section
- **Config via file**: currently only WEBHOOK_ADMIN_KEY is env-configurable; backoff delays, max attempts, poll interval should also be env vars
