# Tasks: In-Memory LRU Cache Service

Derived from `requirements.md` + `design.md`. Tasks are grouped by component, following the bottom-up
build order (`models.py` → `database.py` → `repository.py` → `service.py` → `main.py`). Each task lists
its **files touched**, **prerequisites** (for topological/layered execution), and **acceptance criteria**
tied back to requirement/design sections. No task should require touching files outside its own list —
this keeps each unit executable in isolation.

Status legend: `[ ]` not started, `[x]` done. Update checkboxes as tasks complete.

## Task Graph (quick reference)

| Task                              | Depends on | Files                                                                                        |
| --------------------------------- | ---------- | -------------------------------------------------------------------------------------------- |
| T1 — Data & API schemas           | —          | `backend/app/models.py`, `backend/tests/test_models.py`                                      |
| T2 — Database engine/session      | —          | `backend/app/database.py`                                                                    |
| T3 — Repository layer             | T1, T2     | `backend/app/repository.py`, `backend/tests/conftest.py`, `backend/tests/test_repository.py` |
| T4 — Cache service (domain logic) | T3         | `backend/app/service.py`, `backend/tests/test_service.py`                                    |
| T5 — API layer                    | T1, T2, T4 | `backend/app/main.py`, `backend/tests/test_main.py`                                          |
| T6 — Full-suite verification      | T1–T5      | none (verification only)                                                                     |

T1 and T2 have no interdependency and can be done in either order (or in parallel by two separate
sub-tasks) since they touch disjoint files.

---

## T1 — Data & API Schemas (`models.py`)

**Prerequisites:** none.
**Files touched:** `backend/app/models.py`, `backend/tests/test_models.py`.

- [x] **T1.1 — `CacheRecord` SQLModel table**: `key: str` (primary key), `value: str` (JSON-serialized),
      `expires_at: datetime | None`, `updated_at: datetime`. (design.md §3.1)
- [x] **T1.2 — `CacheSetRequest` Pydantic schema**: `value: Any`, `ttl_seconds: int | None`, with
      validators rejecting `ttl_seconds <= 0` and serialized `value` > 64KB. (requirements.md §1.5)
- [x] **T1.3 — `CacheResponse` Pydantic schema**: `key: str`, `value: Any`.
- [x] **T1.4 — Unit tests (`test_models.py`)**: valid payload passes; oversized value rejected;
      zero/negative `ttl_seconds` rejected. Skip testing `CacheRecord` table mapping directly (no logic
      to verify in isolation — covered indirectly by T3 tests).

**Acceptance criteria:** `pytest backend/tests/test_models.py` passes; no dependency on `database.py`
or FastAPI app import required for this test file.

---

## T2 — Database Engine & Session (`database.py`)

**Prerequisites:** none.
**Files touched:** `backend/app/database.py`.

- [x] **T2.1 — Engine**: SQLite engine (`sqlite:///./cache.db`, `connect_args={"check_same_thread":
    False}`).
- [x] **T2.2 — `create_db_and_tables()`**: creates tables from `SQLModel.metadata` (called later from
      `main.py` lifespan in T5).
- [x] **T2.3 — `get_session()`**: generator dependency yielding a `Session(engine)` per call, with
      proper close/cleanup.

**Acceptance criteria:** module imports cleanly; no dedicated test file (design.md §3.2) — correctness
is verified implicitly by T3's repository tests, which will use a test engine/session fixture.

---

## T3 — Repository Layer (`repository.py`)

**Prerequisites:** T1 (needs `CacheRecord`), T2 (needs engine/session for test fixtures).
**Files touched:** `backend/app/repository.py`, `backend/tests/conftest.py` (add DB session fixture),
`backend/tests/test_repository.py`.

- [x] **T3.1 — `CacheRepository(BaseRepository[CacheRecord])`**: add `upsert(db, *, key, value,
    expires_at) -> CacheRecord` (get by PK, update in place, or create). Reuse inherited `get`/`delete`
      from `BaseRepository` unchanged — do not duplicate them.
- [x] **T3.2 — Test fixture (`conftest.py`)**: a test-scoped SQLite session (e.g., temp-file or
      `sqlite://` in-memory engine + `create_db_and_tables()`), isolated per test.
- [x] **T3.3 — Unit tests (`test_repository.py`)**: `upsert` creates a new row then updates an existing
      one; `get` returns `None` for a missing key; `delete` returns `False` for a missing key. Skip
      re-testing generic `BaseRepository` methods already implied correct by existing code — only cover
      `upsert` and the exact behaviors `CacheService` (T4) will depend on.

**Acceptance criteria:** `pytest backend/tests/test_repository.py` passes against a real (test) SQLite
DB — no mocking of the DB layer itself.

---

## T4 — Cache Service / Domain Logic (`service.py`)

**Prerequisites:** T3 (calls `CacheRepository`).
**Files touched:** `backend/app/service.py`, `backend/tests/test_service.py`.

- [x] **T4.1 — State & `CacheEntry`**: `CacheEntry` dataclass (`value`, `expires_at`);
      `CacheService.__init__` with `_store: OrderedDict[str, CacheEntry]`, `_lock: asyncio.Lock`,
      `_max_size` (from `CACHE_MAX_SIZE` env, default 10000).
      **Revised during implementation** (see design.md §2.1): originally spec'd as `dict` + `deque` +
      `seq` counter with lazy invalidation, but that risked unbounded deque growth if the cache never
      reached capacity. Collapsed into a single `OrderedDict` instead — `move_to_end`/`popitem` give
      genuine O(1) reordering/eviction with no separate recency structure to reconcile or clean up.
- [x] **T4.2 — Recency/eviction helper**: `_evict_if_needed(key)` calling
      `self._store.popitem(last=False)` while at capacity (evicts the true LRU key directly — no
      staleness bookkeeping needed since `OrderedDict` has exactly one entry per key).
- [x] **T4.3 — `async def get(session, key)`**: in-memory hit (not expired) → `move_to_end` + return;
      expired → purge + fall through; miss → best-effort read-through via `CacheRepository`
      (`try/except` → log, treat as miss on failure), populate `_store` (with eviction) on DB hit.
- [x] **T4.4 — `async def set(session, key, value, ttl_seconds)`**: evict via `_evict_if_needed` if
      inserting a new key at capacity, write to `_store`, `move_to_end`, best-effort write-through via
      `CacheRepository.upsert` (`try/except` → log, never raise).
- [x] **T4.5 — `async def delete(session, key) -> bool`**: remove from `_store` if present, best-effort
      delete via repository, return whether the key existed.
- [x] **T4.6 — `async def purge_expired(session)`**: sweep + remove expired entries directly from
      `_store`; best-effort delete matching rows from SQLite. (Consumed by the background
      task wired up in T5.)
- [x] **T4.7 — Unit tests (`test_service.py`)**:
  - LRU: inserting beyond `_max_size` evicts the correct (least-recently-used) key.
  - Recency refresh: a `get` on an existing key protects it from the next eviction.
  - TTL: expired entry is a miss on `get`; `purge_expired` removes it.
  - Fail-open: repository raising on read-through/write-through does not raise out of `get`/`set`.
  - Repeated-touch correctness: repeated touches of the same key still preserve correct LRU order
    for other keys.
  - Skip: concurrency/stress and latency benchmarks (out of scope per 3h budget, per design.md §3.4).

**Acceptance criteria:** `pytest backend/tests/test_service.py` passes using `CacheRepository` against a
real test DB (reuse T3's fixture) — no need to mock the repository given how fast/cheap SQLite is here.

---

## T5 — API Layer (`main.py`)

**Prerequisites:** T1 (schemas), T2 (`create_db_and_tables`/`get_session`), T4 (`CacheService`).
**Files touched:** `backend/app/main.py`, `backend/tests/test_main.py`.

- [x] **T5.1 — Lifespan wiring**: on startup, call `create_db_and_tables()`, instantiate a single
      module-level `CacheService`, start the background sweep task (`asyncio.create_task`, loop +
      `asyncio.sleep(30)` calling `purge_expired`); cancel the task on shutdown.
- [x] **T5.2 — Routes**:
  - `PUT /cache/{key}` (`Path(min_length=1, max_length=256)`, body `CacheSetRequest`) → `CacheResponse`,
    `200`.
  - `GET /cache/{key}` (same `Path` constraint) → `CacheResponse` on hit, `HTTPException(404)` on miss.
  - `DELETE /cache/{key}` → `204` on success, `HTTPException(404)` if key did not exist.
  - Keep existing `GET /health` unchanged; `setup_observability(app)` stays wired as-is.
- [x] **T5.3 — Integration tests (`test_main.py`, `TestClient`)**:
  - `SET` then `GET` round-trip returns the same value.
  - `GET` on unknown key → `404`.
  - `SET` beyond `CACHE_MAX_SIZE` evicts the correct key.
  - Restart/read-through simulation: after a `SET`, a fresh `CacheService` bound to the same DB proves
    `GET` still returns the value (requirements.md §1.4).
  - Oversized value / bad `ttl_seconds` / oversized key → `400`/`422`.
  - `DELETE` existing key → `204`, then `GET` → `404`; `DELETE` missing key → `404`.
  - Skip: auth, rate-limiting, multi-instance tests (requirements.md §4, out of scope).

**Acceptance criteria:** `pytest backend/tests/test_main.py` passes; existing `test_health_check` in
`test_main.py` still passes unmodified.

---

## T6 — Full-Suite Verification

**Prerequisites:** T1, T2, T3, T4, T5.
**Files touched:** none (verification-only task).

- [ ] Run the full suite: `pytest` from the repo root — all tests across T1–T5 pass together.
- [ ] Quick manual smoke check (optional, time-permitting): run `uvicorn backend.app.main:app` locally
      and `curl` through a `PUT`/`GET`/`DELETE` cycle plus a `GET /health` to eyeball real responses.
