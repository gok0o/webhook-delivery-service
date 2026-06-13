# Architectural Decisions

### Storage Choice: SQLite
I went with SQLite using Python's native `sqlite3` module. 
* **Alternatives considered:** PostgreSQL (adds operational overhead and setup steps) and Redis (great for queues, but bad for permanent, relational logs). Pure in-memory was skipped because data must survive app restarts.
* **Why:** It requires zero configuration, stores everything in a single local file, and easily satisfies the persistence constraint. By enabling WAL (Write-Ahead Logging) mode, I bypassed SQLite's standard file-locking limitations, allowing the background worker to continuously update webhook logs while the FastAPI server freely accepts new events.

### Concurrency & Background Processing
The service runs a single native `asyncio.Task` managed directly inside FastAPI's startup/shutdown lifespan wrapper, polling the database every 5 seconds.
* **Alternatives considered:** Separate worker processes (adds IPC and database sharing complexity) or Celery (adds massive dependencies like Redis/RabbitMQ).
* **Why:** FastAPI is built entirely on async principles. Creating a background task via the event loop keeps the codebase unified, zero-dependency, and runnable via a single command. Inside the polling loop, `asyncio.gather` handles all ready-to-deliver HTTP requests concurrently. A slow or hanging subscriber won't bottleneck other deliveries.
* **At-Least-Once Semantics:** An event attempt is explicitly marked as `pending` or `retrying` in the DB. If the application crashes or restarts while an HTTP request is mid-flight, the record remains uncommitted or pending. On reboot, the worker picks it right back up. This means a subscriber might receive a duplicate webhook if we crash right after sending the payload but right before saving the success state—which is the industry-standard design tradeoff for webhooks.

### Retry Logic & Exponential Backoff
Failed deliveries attempt a maximum of 5 tries, utilizing fixed exponential intervals: Immediate, 30s, 5m, 30m, and 2h.
* **Alternatives considered:** Fixed intervals (leads to "retry storms" that hammer down servers) or infinite retries (clogs the database queue with dead URLs forever).
* **Why:** Backoff prevents our worker from worsening a subscriber's server outage. I separated failures into two buckets: permanent 4xx errors (bad payloads, configuration issues) stop retrying immediately since code changes are needed to fix them. On the other hand, 5xx errors, network timeouts, 408s, and 429s trigger retries because they represent transient, temporary failures.

### Payload Security & Signing
Every outbound request body is signed using HMAC-SHA256, exposed via the `X-Webhook-Signature` header.
* **Alternatives considered:** Asymmetric cryptography (RSA keys, which complicates public key rotation for subscribers) or no signature at all (unacceptable for secure webhook verification).
* **Why:** It is the industry standard used by Stripe and GitHub. It's lightweight and secure. The signature is computed directly over the raw encoded JSON bytes—not a parsed string dictionary—ensuring the signature matches exactly what hits the wire. On the verification side, I included `hmac.compare_digest` to mitigate timing-attack exploits.

### Dashboard Architecture
Built with plain server-side Jinja2 templates and minimal vanilla JavaScript for handling action buttons.
* **Alternatives considered:** Single Page Applications (React/Vue require complex build steps, node_modules, and tooling) or HTMX (nice, but introduces an extra external library dependency).
* **Why:** The assignment explicitly stated that CSS wasn't graded and a simple table/detail view was sufficient. Server-side rendering with Jinja keeps the template footprint incredibly lightweight, completely removes the need for a separate frontend build process, and natively fulfills all requirements (browsing events, diagnosing failures, and executing manual retries).