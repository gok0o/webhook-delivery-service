# DECISIONS.md

## Storage

**Choice**: SQLite via Python's built-in `sqlite3` module with WAL mode.

**Alternatives considered**: PostgreSQL (overkill for a single-process service; adds a hard dependency), Redis (fast but would need a second store for persistent event records), pure in-memory (explicitly ruled out by the spec).

**Why SQLite**: Zero configuration, single file, survives restarts, and handles the concurrency requirements of this service fine in WAL mode. WAL allows the background worker to write delivery results at the same time the API server is reading event lists — the key concurrency concern here. The data model is relational (subscriptions → events → attempts with FK constraints), which SQLite handles well. For a single-process service receiving moderate webhook volumes, SQLite will not be a bottleneck.

---

## Concurrency / Worker model

**Choice**: `asyncio.Task` running in the same process as FastAPI, polling the database every 5 seconds.

**Alternatives considered**: A separate worker process (adds IPC complexity, shared DB state, process management); thread pool (mixing threads and asyncio is error-prone); Celery/RQ (heavy dependencies, overkill for the scope). 

**Why asyncio task**: FastAPI is already async. Launching a co-routine as a background task via `asyncio.create_task` in the app lifespan is idiomatic, zero-dependency, and keeps the service runnable with a single command. Within each poll cycle, all pending deliveries fire concurrently via `asyncio.gather`, so a slow subscriber doesn't block others. The trade-off: if the event volume grows very large, a single worker loop might fall behind — at that point you'd graduate to a dedicated worker process or queue.

**At-least-once semantics**: An attempt row is in `pending` or `retrying` status until we record a definitive outcome (success or permanent failure). If the process crashes mid-delivery, the attempt row is still in `pending`/`retrying` on restart, and the worker picks it up again. This means a subscriber *may* receive the same event twice if we crash after sending the HTTP request but before writing `status=success`. This is the standard webhook trade-off and is documented to callers.

---

## Retry policy

**Choice**: 5 attempts max, with delays of immediate / 30s / 5min / 30min / 2hr. Don't retry on non-transient 4xx.

**Alternatives considered**: Fixed interval (simpler, but causes retry storms when a subscriber is down — all retries arrive at the same time); jitter (good production practice, skipped here to keep the code readable); infinite retries (dangerous — dead subscribers accumulate unboundedly).

**Why this policy**: Exponential backoff reduces pressure on a struggling subscriber. The max-attempt cap prevents unbounded retry accumulation. The 4xx rule distinguishes "the subscriber rejected our payload" (probably a code bug — retrying won't help) from "the subscriber is temporarily unavailable" (5xx, network errors, 408, 429 — retrying will help). The spec explicitly calls 408/429 out as retry candidates; the spec also suggests this rule is "guidance, not gospel", so the code documents the call.

---

## Payload signing

**Choice**: HMAC-SHA256. Header: `X-Webhook-Signature: sha256=<hex_digest>`. Signature is over the raw JSON bytes of the request body.

**Alternatives considered**: Asymmetric signing (RSA/ECDSA) — stronger but requires subscribers to manage a public key; no signing at all — acceptable for internal services, bad practice for anything subscriber-facing.

**Why HMAC-SHA256**: This is the industry standard (GitHub, Stripe, Shopify all use it). Symmetric keys are simple to share (one secret string), and HMAC is computationally cheap. Signing the raw body bytes (not a parsed dict) ensures the signature covers exactly what the subscriber receives. `hmac.compare_digest` is used for verification to prevent timing attacks.

---

## Dashboard scope

**Choice**: Three pages — event list (index), event detail with delivery attempts table, subscription manager with create/delete.

**Alternatives considered**: Single-page app with React (too much build tooling for the scope); server-side with htmx (nice but adds a library); plain Jinja2 with minimal JS (chosen).

**Why this scope**: The spec says "a table view and a detail view is enough" and explicitly says CSS won't be graded. The dashboard covers the four stated requirements: list subscriptions, browse recent events, drill into delivery attempts, manually retry. JS is used only for the retry/create/delete buttons that need to call the API with the admin key — all page rendering is server-side Jinja2. This keeps the templates simple and the feature complete.
