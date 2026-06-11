# AI_LOG.md

Five meaningful interactions during development.

---

## 1. Retry policy — should 4xx always skip retry?

**Asked**: "The spec says 4xx (except 408/429) means don't retry. But what if a subscriber temporarily returns 401 because they rotated their credentials — shouldn't I retry that?"

**Got back**: A nuanced breakdown: 401 and 403 are almost always permanent at the webhook level because the delivery service can't fix an auth problem without human intervention. The subscriber would need to update their subscription secret or fix their endpoint. Retrying adds noise without helping. The exception to the exception (408/429) is about *capacity* — the subscriber is reachable but temporarily overloaded or timing out, which backoff addresses well.

**Kept**: The `NO_RETRY_STATUS_CODES = set(range(400, 500)) - {408, 429}` approach. I added a comment in the code explaining the reasoning rather than leaving it as a bare constant.

---

## 2. asyncio worker vs thread pool

**Asked**: "I'm thinking of running the delivery worker in a thread pool executor instead of as an asyncio task. Any issues?"

**Got back**: A clear warning: mixing `asyncio` (used by FastAPI) with threads for I/O tasks is usually the wrong call. `httpx` has a native async client (`httpx.AsyncClient`) that integrates cleanly with asyncio. Using a thread pool would mean wrapping async calls in `asyncio.run_coroutine_threadsafe`, which is more complex and doesn't actually get you more parallelism for I/O-bound work.

**Kept**: The asyncio task approach. **Rejected**: The suggestion to add `asyncio.Semaphore` to cap concurrent deliveries per subscriber — good idea but I noted it in README as a future improvement rather than adding complexity mid-scope.

---

## 3. SQLite WAL mode — is it actually necessary?

**Asked**: "Do I need WAL mode for SQLite if I'm in a single process?"

**Got back**: Yes, for this specific pattern — concurrent reads during a write. Without WAL, SQLite uses a reader-writer lock and a write blocks all readers. With a background worker writing attempt outcomes at the same time the API is reading event lists for the dashboard, you'd get brief but real lock contention. WAL mode allows concurrent reads during writes at the cost of a slightly larger file footprint (the `-wal` and `-shm` files).

**Kept**: `PRAGMA journal_mode=WAL` added to `get_conn()`. Also added `PRAGMA foreign_keys=ON` which the AI pointed out I was missing — it's off by default in SQLite.

---

## 4. Event filter matching implementation

**Asked**: "I want subscriptions to match `*`, `order.*`, or `order.created`. What's the cleanest Python implementation?"

**Got back**: Two approaches — (a) store a compiled regex per subscription and match at query time in Python, (b) simple string logic since the pattern language is fixed and small.

**Kept**: Option (b). Three `if/elif` checks in Python after fetching all active subscriptions. The AI's regex suggestion was more general but added complexity that wasn't needed — the spec defines exactly three filter shapes and they're trivially matchable with string comparison. I noted this explicitly in `database.py` with comments on what each branch handles.

**Rejected**: The suggestion to do the filtering in SQL with a `LIKE` clause. This would work for `*` and exact match but breaks for `order.*` semantics (you'd need `LIKE 'order.%'` which incorrectly matches `orders.created`).

---

## 5. Dashboard retry button and API key exposure

**Asked**: "The dashboard retry button needs to call the API with the admin key. Is it safe to hardcode it in the template JS?"

**Got back**: Honest answer: no, not for production. The key would be visible in page source to anyone who can load the dashboard. Suggested alternatives: (a) a separate session-authenticated endpoint that proxies the retry without exposing the key, (b) a cookie-based session on the dashboard routes, (c) accept the risk if the dashboard is behind a network boundary (internal tool only).

**Kept**: The hardcoded key in the template JS, with a comment explaining it's only acceptable because the spec explicitly says "a single shared admin key is fine" and "no authentication" is a non-goal. I would address this before any real deployment — noted in README under "would improve with more time."
