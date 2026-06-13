# AI Log - Webhook Service Development

This log tracks a few of the critical design calls where I used an LLM to bounce ideas off of, check edge cases, or validate my approach.

### 1. Handling 4xx Errors vs Retries
* **What I asked:** The prompt says 4xx means don't retry, except for 408 and 429. But what if a subscriber has a temporary authentication issue (like a stale 401)? Should I retry that?
* **The advice:** Stick to the prompt's rule. 401 or 403 errors usually mean an explicit authorization failure. A script can't magically fix an expired token or mismatched secret without human intervention—retrying just hammers their server with useless traffic. 408 (Timeout) and 429 (Rate Limited) are capacity issues, so those *should* use backoff.
* **What I did:** Kept the exact exception rule (`set(range(400, 500)) - {408, 429}`). I added a clear comment block in `worker.py` explaining this so anyone reading the code understands the reasoning.

### 2. Worker Concurrency Model (Async vs Threads)
* **What I asked:** I'm using FastAPI. Should I spin up the background worker using a thread pool executor or just keep it as a native asyncio task?
* **The advice:** Don't mix raw threads with asyncio for standard network I/O. Since we use `httpx.AsyncClient`, it plays perfectly with the event loop. Throwing a thread pool into the mix forces you to manage thread-safe event loop bridges (`asyncio.run_coroutine_threadsafe`), adding complexity for zero performance gain.
* **What I did:** Stuck with a clean `asyncio.create_task` inside the app's lifespan handler. The AI also suggested adding an `asyncio.Semaphore` to limit concurrent connections to individual endpoints. I liked the idea but rejected implementing it right now to prevent over-engineering the initial scope; added it to the README under future improvements instead.

### 3. SQLite Concurrency (WAL Mode)
* **What I asked:** Since this entire app runs inside a single process, do I actually need to bother enabling WAL (Write-Ahead Logging) mode for SQLite?
* **The advice:** Yes. Even in a single process, FastAPI handling incoming API requests (writes) while the background worker loops through old delivery logs (reads) will trigger database lock contention. WAL mode completely untangles this by letting readers access data while a write transaction is mid-flight.
* **What I did:** Added `PRAGMA journal_mode=WAL` to the connection function. The AI also noticed I hadn't explicitly turned on foreign keys (which SQLite disables by default), so I added `PRAGMA foreign_keys=ON` right next to it.

### 4. Event Filter Processing
* **What I asked:** I need to match filters like `*`, `order.*`, or `order.created`. Should I write a regex engine or handle this at the SQL query level?
* **The advice:** Overkill. The patterns are highly predictable. You can handle this with basic string splitting and matching directly in Python after pulling active subscriptions.
* **What I did:** Wrote a simple `if/elif` block in `get_matching_subscriptions`. I completely rejected the AI's secondary suggestion to use SQL `LIKE` operators, because a SQL query like `LIKE 'order.%'` would accidentally match an event type named `orders.created` (plural), breaking strict routing.

### 5. Frontend Security vs The Admin Key
* **What I asked:** The dashboard retry button calls the API. Is it fine to just pass the admin key header inside the template's vanilla JavaScript fetch call?
* **The advice:** It's insecure for production because anyone viewing the page source can steal the key. A real app would use session cookies or an internal backend proxy route.
* **What I did:** I decided to keep the key in the script anyway. The assignment explicitly noted that a single shared key is acceptable and real RBAC/Auth is a non-goal. I added a prominent comment in the template explaining this tradeoff and documented it in the README as a primary production upgrade.