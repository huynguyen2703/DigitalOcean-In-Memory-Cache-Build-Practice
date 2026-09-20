# Design: In-Memory LRU Cache Service

Companion to `requirements.md`. Defines architecture, data structures/algorithms, and a bottom-up
build order (`models.py` → `repository.py` → `service.py` → `main.py`), each with its testing scope,
sized for a 3-hour build.

## 1. System Architecture

The service runs as a single FastAPI process with one linear request path and one background task:

1. **Client → `ObservabilityMiddleware`**: every HTTP request first passes through the existing
   middleware, which assigns/propagates `X-Trace-Id` and records timing (already implemented in
   `observability.py`, unchanged).
2. **`main.py` routes**: `PUT/GET/DELETE /cache/{key}` (plus the existing `GET /health`). The route
   layer validates the `key` path parameter (`Path(min_length=1, max_length=256)`) and the request
   body (`CacheSetRequest` Pydantic schema), then delegates to the service layer.
3. **`service.py :: CacheService`**: the single domain-logic component. Under `async with self._lock:`
   it owns one in-memory primitive — `OrderedDict[str, CacheEntry]`, which provides O(1) lookup _and_
   O(1) recency reordering (`move_to_end`/`popitem`) in a single structure — and decides whether a
   request is satisfied purely in-memory or needs to fall through to persistence (on miss,
   write-through, or delete).
4. **`repository.py :: CacheRepository`**: a thin persistence-access layer (extends
   `BaseRepository[CacheRecord]` with an `upsert`) invoked only by `CacheService`. Requests never talk
   to SQLite directly.
5. **`database.py :: SQLModel Session → SQLite`**: the actual storage engine/session used by the
   repository layer.
6. **Background sweep task**: an `asyncio` task, started in `main.py`'s `lifespan` and running roughly
   every 30 seconds, calls back into the same `CacheService` instance's `purge_expired()` method — it
   shares the same lock and state as the request path, it does not run in a separate process.

`CacheService` is therefore the single boundary that owns both the fail-open persistence policy and
the concurrency lock; nothing upstream or downstream of it touches shared cache state directly.

## 2. Primitive Mapping

| Concern                        | Primitive                                                                 | Rationale                                                                                                                                                                                                                                                                             |
| ------------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Key → value lookup + recency   | `collections.OrderedDict[str, CacheEntry]`                                | A single structure gives O(1) average lookup _and_ genuine O(1) reordering: `move_to_end(key)` and `popitem(last=False)` relink pointers in the underlying linked hash-map — no scanning, no stale entries, no separate bookkeeping structure needed.                                 |
| Cross-request mutual exclusion | `asyncio.Lock` (single, per `CacheService` instance)                      | Cache mutation spans multiple steps (check dict → maybe hit DB → update dict); the whole sequence must be atomic per key operation. A single process-wide lock is sufficient because this is a single-node, single-worker service (2.3) — no striping/sharding needed for a 3h build. |
| Durability                     | SQLite via SQLModel `Session`                                             | Already-required secondary store; reuses the existing fail-open `BaseRepository` pattern in `repository.py`.                                                                                                                                                                          |
| TTL expiry sweep               | `asyncio` background task (`asyncio.create_task`, loop + `asyncio.sleep`) | No external scheduler; matches "single-node process only" constraint.                                                                                                                                                                                                                 |

### 2.1 Design Revision: `OrderedDict` instead of `deque` + lazy invalidation

An earlier iteration of this design used a plain `dict` plus a `collections.deque` for recency order,
with a monotonic `seq` counter to lazily distinguish "fresh" vs "stale" deque entries (since a
`deque` has no O(1) `remove(key)` from the middle). That approach is amortized O(1), but it has a
real flaw: stale `(key, seq)` tuples are only ever cleaned up as a side effect of an eviction actually
happening. If the cache never reaches `_max_size` (e.g., a small number of hot keys touched
repeatedly), the deque grows **unboundedly** relative to total touches, even though the `dict` itself
stays correctly bounded — undermining the predictable memory ceiling required by requirements §2.2.

**Resolution:** collapse `dict` + `deque` + `seq` into a single `collections.OrderedDict`. Internally,
`OrderedDict` is a hash table plus a doubly-linked list threading through the entries in order —
exactly the "hash map + doubly linked list" structure the LRU pattern calls for, already implemented
in the standard library:

- Every `GET` hit or `SET` touch calls `self._store.move_to_end(key)`, which unlinks that key's
  existing node and relinks it at the tail — O(1) pointer operations, and the key's entry is _moved_,
  never duplicated.
- Eviction calls `self._store.popitem(last=False)`, which unlinks and returns the head node (the true
  least-recently-used key) — O(1), no staleness check required since there is only ever one entry per
  key.
- Cost: genuine O(1) per operation (not merely amortized), and total memory is always exactly
  bounded by the number of live entries — no compaction step, no unbounded growth path.

This does mean `CacheEntry` no longer needs a `seq` field, and `CacheService` no longer needs a
separate `_recency` deque or `_seq` counter.

## 3. Bottom-Up Implementation Plan

### 3.1 `models.py` — DB schema + API contracts

- `CacheRecord(SQLModel, table=True)`: `key: str` (primary key, indexed), `value: str` (JSON-serialized
  payload), `expires_at: datetime | None`, `updated_at: datetime`.
- `CacheSetRequest(BaseModel)`: `value: Any`, `ttl_seconds: int | None`.
  - Field validator: `ttl_seconds` must be `> 0` if provided → else raises (FastAPI turns this into `422`,
    which the service/tests treat as the `400`-class validation failure required by requirements §1.5).
  - Field validator: serialized `value` size ≤ 64KB (via `json.dumps` length check).
- `CacheResponse(BaseModel)`: `key: str`, `value: Any`.
- **Tests (`test_models.py`, unit, no DB/app needed):** valid payload passes; oversized value rejected;
  negative/zero `ttl_seconds` rejected. Skip testing `CacheRecord` table mapping directly — it has no
  logic and is exercised indirectly by repository tests.

### 3.2 `database.py` — engine & session lifecycle

- Single SQLite engine (`sqlite:///./cache.db`, `connect_args={"check_same_thread": False}`).
- `create_db_and_tables()` called once from `main.py` lifespan startup.
- `get_session()` generator dependency yielding a `Session(engine)` per request (used by routes that
  need direct DB access, if any — primarily consumed internally by `CacheService`/`CacheRepository`).
- **Tests:** none dedicated — implicitly covered by repository/integration tests using a test engine
  fixture (e.g., `sqlite://` in-memory or temp-file DB) in `conftest.py`. Not worth isolated unit tests
  for a thin engine/session factory.

### 3.3 `repository.py` — persistence access

- Add `CacheRepository(BaseRepository[CacheRecord])` alongside the existing generic base:
  - `upsert(db, *, key, value, expires_at) -> CacheRecord` (get by PK, update in place, or create).
  - Reuses inherited `get` (by PK), `delete`, from `BaseRepository` as-is — no need to duplicate.
- Keeps the existing fail-open `try/except: rollback; raise` pattern in `BaseRepository` unchanged;
  `CacheRepository` does not swallow exceptions itself — that responsibility belongs to the caller
  (`CacheService`), which decides fail-open policy per requirements §2.4.
- **Tests (`test_repository.py`, unit, real test DB session):** `upsert` creates then updates a row;
  `get` returns `None` for missing key; `delete` returns `False` for missing key. Skip testing generic
  `BaseRepository` methods already implied correct by existing code — only test the new `upsert` logic
  and the exact behaviors `CacheService` depends on.

### 3.4 `service.py` — domain logic

- `CacheEntry` (lightweight `dataclass`): `value: Any`, `expires_at: datetime | None`.
- `CacheService`:
  - State: `_store: OrderedDict[str, CacheEntry]`, `_lock: asyncio.Lock`, `_max_size: int` (from
    `CACHE_MAX_SIZE` env, default 10000).
  - `async def get(session, key) -> Any | None`: under lock — check `_store`; if present and not
    expired, `move_to_end` and return value; if expired, purge and fall through; if absent,
    best-effort read-through from `CacheRepository` (wrapped in `try/except` → log + treat as miss on
    failure), and if found, insert into `_store` (respecting eviction) before returning.
  - `async def set(session, key, value, ttl_seconds) -> None`: under lock — write to `_store` (evicting
    LRU first if inserting a new key at capacity), `move_to_end`, then best-effort write-through via
    `CacheRepository.upsert` (`try/except` → log, do not raise).
  - `async def delete(session, key) -> bool`: under lock — remove from `_store` if present (record
    whether it existed), best-effort delete via repository, return whether the key existed.
  - `async def purge_expired(session) -> None`: under lock — remove expired entries directly from
    `_store`; called by the background sweep task, and best-effort deletes the matching rows from
    SQLite.
  - `_evict_if_needed()`: internal helper calling `self._store.popitem(last=False)` while at capacity.
- **Tests (`test_service.py`, unit, `CacheService` instantiated directly with a stubbed/real repository):**
  - LRU order: inserting beyond `_max_size` evicts the correct (least-recently-used) key.
  - Recency refresh: a `GET` on an existing key protects it from the next eviction.
  - TTL: expired entry is a miss on `get`; `purge_expired` removes it.
  - Fail-open: repository raising on write-through/read-through does not raise out of `set`/`get`.
  - Repeated-touch correctness: repeatedly touching the same key still preserves correct LRU order
    for other keys (regression coverage for the `move_to_end` reordering behavior).
  - Skip: dedicated concurrency/stress tests (e.g., hundreds of concurrent tasks) and performance/latency
    benchmarks — out of scope per the 3-hour budget; correctness of the lock boundary is covered by
    code review + the single-lock design, not load testing.

### 3.5 `main.py` — API layer

- Extend the existing `FastAPI` app (keep `GET /health`) with:
  - `PUT /cache/{key}` (`key: str = Path(..., min_length=1, max_length=256)`, body `CacheSetRequest`) →
    `CacheResponse`, `200`.
  - `GET /cache/{key}` (same `Path` constraint) → `CacheResponse` on hit, `HTTPException(404)` on miss.
  - `DELETE /cache/{key}` → `204` on success, `HTTPException(404)` if key did not exist.
- `lifespan`: call `create_db_and_tables()`, instantiate the single module-level `CacheService`, start
  the background sweep task (`asyncio.create_task`, loop with `asyncio.sleep(30)` calling
  `purge_expired`), cancel the task on shutdown.
- Reuses `setup_observability(app)` (already implemented) and `Depends(get_session)` for the DB session
  passed into `CacheService` calls.
- **Tests (`test_main.py`, integration, `TestClient`):**
  - `SET` then `GET` round-trip returns the same value.
  - `GET` on unknown key → `404`.
  - `SET` beyond `CACHE_MAX_SIZE` evicts the correct key (assert evicted key now 404s in-memory but is
    still recoverable — see next point).
  - Restart/read-through simulation: after a `SET`, construct a _new_ `CacheService` bound to the same
    DB file/engine (simulating a cold cache) and confirm `GET` still returns the value (proves hybrid
    persistence, requirements §1.4).
  - Oversized value / bad `ttl_seconds` / oversized key → `400`/`422`.
  - `DELETE` existing key → `204`, then `GET` → `404`; `DELETE` missing key → `404`.
  - Skip: auth, rate-limiting, multi-instance tests — explicitly out of scope (requirements §4).

## 4. API & Data Contracts

| Method | Path           | Request           | Success                    | Failure                 |
| ------ | -------------- | ----------------- | -------------------------- | ----------------------- |
| PUT    | `/cache/{key}` | `CacheSetRequest` | `200 OK` → `CacheResponse` | `400/422` invalid input |
| GET    | `/cache/{key}` | —                 | `200 OK` → `CacheResponse` | `404` miss              |
| DELETE | `/cache/{key}` | —                 | `204 No Content`           | `404` missing           |
| GET    | `/health`      | —                 | `200 OK`                   | —                       |

SQLite schema (`CacheRecord`): `key TEXT PRIMARY KEY`, `value TEXT` (JSON string), `expires_at
TIMESTAMP NULL`, `updated_at TIMESTAMP`.

## 5. Concurrency & Failure Boundaries

- **Locking:** one `asyncio.Lock` per `CacheService` instance guards the entire critical section of
  `get`/`set`/`delete`/`purge_expired`, including any DB fallback call, so no interleaving can corrupt
  `_store`.
- **Sync DB calls inside async methods:** `SQLModel`'s `Session` is synchronous; calls are made directly
  (no thread offload) since local SQLite I/O is fast and this is a single-worker, single-node service —
  an explicit, documented simplification for the 3-hour budget (a future iteration could move to
  `run_in_threadpool`/`aiosqlite`).
- **Fail-open boundary:** `CacheService` wraps every `CacheRepository` call in `try/except Exception`,
  logs a warning with the trace id, and continues using the in-memory result — persistence failures
  never surface as `5xx` to the client.
- **Race mitigation for read-through:** because the whole `get()` (miss-check → DB read → cache
  populate) runs under the single lock, two concurrent `GET`s for the same missing key cannot both
  read-through and double-insert.

## 6. Deployment (DigitalOcean App Platform)

- **Container hardening (`Dockerfile`):** runs as a non-root `appuser` (CIS Docker Benchmark /
  container-security baseline); a `.dockerignore` excludes tests, docs, VCS metadata, and any local
  `.db`/`.env` files from the build context so they can never leak into the image; base image pinned
  to `python:3.13-slim` (matches the dev environment, gets current security patches).
- **Single-instance constraint:** `CacheService`'s `OrderedDict` and `asyncio.Lock` live in one
  process's memory — there is no shared/external cache backend. `app.yaml` pins `instance_count: 1`
  and the container always runs a single `uvicorn` process (no `--workers`); scaling either would
  silently split traffic across independent, inconsistent caches. This is a hard constraint of the
  current architecture, not a temporary default.
- **Health check / rollback (`app.yaml`):** `health_check` hits the existing `GET /health` endpoint.
  App Platform automatically keeps the previous working deployment live if a new one fails its health
  check, giving basic deploy-time resilience without any custom rollback logic.
- **Config via env:** `CACHE_MAX_SIZE` is set through `app.yaml`'s `envs` (`RUN_TIME` scope) rather
  than hardcoded, so capacity can be tuned per environment without a rebuild.
- **Ephemeral disk (known limitation):** App Platform services have no persistent volume — the
  SQLite file is wiped on every redeploy/restart/instance replacement. Cross-restart durability
  (requirements.md §1.4) therefore only holds within a single running container's lifetime in this
  deployment target. Accepted as out-of-scope to fix for the 3-hour build (would require a managed
  external database); the existing fail-open path already treats a missing/cold DB file as an
  ordinary miss rather than an error, so this degrades gracefully instead of breaking anything.
- **Transport security:** App Platform terminates TLS and redirects HTTP → HTTPS at the edge for the
  assigned domain automatically; no additional app-level configuration is required or added.
