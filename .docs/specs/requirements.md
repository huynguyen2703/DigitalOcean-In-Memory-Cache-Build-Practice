# Requirements: In-Memory LRU Cache Service

## Overview

A single-node FastAPI service exposing HTTP endpoints for a key-value cache backed by an
in-memory LRU (Least Recently Used) eviction policy, with SQLite as a fail-open secondary
persistence layer for durability across restarts and cache misses. Target build time: 3 hours.
Deployment target: DigitalOcean App Platform (single `basic-xxs` instance, no external brokers).

## 1. Functional Requirements

### 1.1 API Contract

- `PUT /cache/{key}` — Upsert (SET) a value.
  - Request body: `{"value": <JSON-serializable>, "ttl_seconds": <int, optional>}`
  - Response: `200 OK` with `{"key": ..., "value": ...}` on success.
  - Writing an existing key overwrites its value/TTL and refreshes it to most-recently-used.
- `GET /cache/{key}` — Retrieve a value (a hit refreshes recency order).
  - Response: `200 OK` with `{"key": ..., "value": ...}` on hit.
  - `404 Not Found` on miss (key absent, expired, or not found in fallback store).
- `DELETE /cache/{key}` — Remove a value from cache and persistence.
  - Response: `204 No Content` on success, `404 Not Found` if key does not exist.
- `GET /health` — existing liveness endpoint (already implemented), unchanged.

### 1.2 Cache Behavior (LRU)

- Fixed-capacity cache; default max size 10,000 entries, configurable via `CACHE_MAX_SIZE` env var.
- On `SET` when the cache is at capacity and the key is new, evict the single least-recently-used
  entry before inserting (synchronous, on the request path).
- `GET` and `SET` both move the accessed/written key to the most-recently-used position.
- Eviction and recency updates must be O(1) average case (drives data-structure choice in design.md).

### 1.3 Time-To-Live (TTL) Expiration

- `SET` may include an optional `ttl_seconds` (positive integer). Omitted = no expiration.
- An expired entry is treated as a cache miss on `GET` (lazy expiration) and is purged at that time.
- A background asyncio loop periodically (e.g., every 30s) sweeps and purges expired entries so
  memory is reclaimed even without read traffic. This loop is also the natural home for a future
  capacity/memory safety-net check, but synchronous LRU eviction (1.2) is the primary enforcement
  mechanism.

### 1.4 Persistence (Hybrid Cache + SQLite)

- `SET` performs a write-through: update in-memory cache first (authoritative for latency), then
  best-effort persist the entry to SQLite.
- `GET` on an in-memory miss performs a read-through: query SQLite; if found (and not expired),
  repopulate the in-memory cache (applying LRU eviction/capacity rules from 1.2) and return the
  value; otherwise return 404.
- `DELETE` removes from both the in-memory cache and SQLite, best-effort on the SQLite side.
- No bulk warm-up of the in-memory cache from SQLite on startup — cache starts cold, and repopulates
  lazily via read-through. This keeps startup fast within the 3-hour build budget.
- **Platform caveat (DigitalOcean App Platform):** App Platform services have no persistent disk —
  local files, including the SQLite file, are wiped on every redeploy, restart, or instance
  replacement. "Durability across restarts" above therefore only holds for the lifetime of a single
  running container, not across App Platform deploys. This is a known, accepted limitation for the
  3-hour build (fixing it would require a managed external database); it degrades gracefully rather
  than breaking anything, since the fail-open design (§2.4) already treats a cold/missing SQLite
  file as an ordinary miss, not a failure.

### 1.5 Input Validation

- `key`: non-empty string, max 256 characters. Violations return `400 Bad Request`.
- `value`: must be JSON-serializable; serialized size capped at 64KB. Violations return `400 Bad Request`.
- `ttl_seconds`: if provided, must be a positive integer. Violations return `400 Bad Request`.
- Validation occurs before any cache/DB mutation (no partial state changes on invalid input).

## 2. Non-Functional Requirements

### 2.1 Latency

- In-memory cache hit/set/delete path (excluding SQLite I/O): target sub-10ms p99 response time.
- SQLite fallback path (read-through miss, or write-through persistence) is best-effort and allowed
  higher latency; it must never block returning a successful in-memory result to the client.

### 2.2 Complexity & Memory Bounding

- `GET`/`SET`/`DELETE`/eviction must be O(1) average time complexity.
- Space bounded by `CACHE_MAX_SIZE` entries; per-entry size bounded by the 64KB value cap and 256-char
  key cap, giving a predictable worst-case memory ceiling appropriate for a `basic-xxs` instance.

### 2.3 Concurrency

- All cache-mutating operations (and reads that may trigger read-through repopulation) are serialized
  through a single `asyncio.Lock` guarding the shared dict + recency-tracking structure, since critical
  sections span `await` points (SQLite I/O) where race conditions could otherwise occur.
- Single-process, single-node execution only — no multi-instance cache coherence (`instance_count: 1`
  per `app.yaml`); this is explicitly out of scope.

### 2.4 Resilience / Fail-Open Persistence

- Any SQLite failure (write-through or read-through) is caught, logged, and does not fail the HTTP
  request or corrupt in-memory cache state — the in-memory result (or a cache miss) is still returned.
- Reuses the existing fail-open `try/except` + rollback pattern already established in
  `backend/app/repository.py`.

### 2.5 Observability

- Reuses existing `X-Trace-Id` propagation and global exception handling from
  `backend/app/observability.py` (no new observability work required).
- Cache hit/miss/eviction/expiry events should be logged (info/debug level) for debuggability.

## 3. Failure & Edge Cases

- Cache miss + SQLite miss → `404 Not Found`.
- SQLite unavailable/corrupted during read-through → treated as cache miss, warning logged, `404`
  returned (never `500` for this case).
- SQLite unavailable during write-through → `SET` still succeeds (in-memory), warning logged.
- Oversized key/value or invalid `ttl_seconds` → `400 Bad Request`, no state mutated.
- Expired entry accessed → treated identically to a normal miss (and purged).
- Concurrent requests for the same key → serialized by the shared lock; no duplicate evictions or
  lost updates.
- Service restart → in-memory cache is empty; individual keys are transparently repopulated on next
  `GET` via SQLite read-through (no full warm-up).

## 4. Out of Scope (Non-Goals for the 3-hour build)

- Multi-instance / distributed cache coherence.
- Authentication, authorization, or rate limiting (no `429` handling).
- `GET /cache/_stats` or any metrics/introspection endpoint.
- Byte-accurate process memory tracking (entry-count capacity is used as the bounding proxy instead).
- Cache warm-up / bulk preload from SQLite on startup.
