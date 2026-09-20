# Role & Mission

You are a Staff Systems Architect. Based on the problem statement, system constraints, paper sketch notes, and technical trade-offs provided below, generate three complete specification files (`requirements.md`, `design.md`, `tasks.md`) inside `.docs/specs/`.

---
ARCHITECTURAL BASELINE & REPOSITORY LAYOUT

- Environment: Single-node Python 3.13 process using FastAPI + Uvicorn.
- Architectural Standard: Production-grade, low-latency, modular system design. Ingestion/HTTP handlers must remain decoupled from long-running or CPU-bound tasks.
- Storage & Resilience: In-memory state tracking as primary source of truth. Secondary persistence (e.g., SQLite via SQLModel) must use fail-open `try/except` boundaries to prevent secondary storage failures from breaking core execution loops.
- Codebase Directory Structure:
  - `backend/app/database.py`: Database engine and session lifecycle dependencies.
  - `backend/app/models.py`: API DTOs (Pydantic) and database entity schemas (SQLModel).
  - `backend/app/observability.py`: Request tracing (`X-Trace-Id`) and global error handling.
  - `backend/app/repository.py`: Storage access layer with fail-open error isolation.
  - `backend/app/service.py`: Core domain logic, state machines, synchronization primitives, and background loops.
  - `backend/app/main.py`: FastAPI endpoints, router mounting, middleware, and lifespan event wiring.
  - `backend/tests/`: Integration and unit tests runnable via `pytest`.

---

INPUT CONTEXT INJECTION

## 1. PROBLEM STATEMENT

I am building a functional service for Digital Ocean Platform. The component is an
in memory cache service run by LRU eviction policy algorithm. This component will be built within 3 hours and deployed on Digital Ocean App Platform.

## 2. SYSTEM CONSTRAINTS & TARGET PRIMITIVES

- External Infrastructure Constraints: [e.g., Single-node process only. No external brokers (No Redis/Kafka/Celery).]
- Target Ingestion SLA: [e.g., Sub-10ms response time on HTTP POST routes.]
- Permitted Memory Primitives: [e.g., dict for state, asyncio.Queue for transport, heapq for retries, collections.deque for sliding windows, asyncio.Lock for per-entity concurrency.]

## 3. PAPER SKETCH & COMPONENT FLOW NOTES

- The service will expose HTTP endpoints for cache operations (`GET`, `SET`, `DELETE`).
- The in-memory cache will be implemented using a dictionary for fast key-value access.
- LRU eviction will be managed using a `collections.deque`(simulate double linked list) to track access order.
- Background loop will periodically check memory usage and trigger eviction if necessary.
- Each cache operation will acquire an `asyncio.Lock` to ensure thread-safe access.
- The service will run as a single-node process on Digital Ocean App Platform, without external brokers.
- SQLite database will be used for persistent storage of cache entries, ensuring data durability across service restarts and cache miss, simulating how a database and in-memory cache can work together in a hybrid storage model.

## 4. TECHNICAL GAPS, EDGE CASES & ARCHITECTURAL DEFENSES

- Notes from developer may contain technical gap, however, you should try to close 
that gap to catch technical tradeoff and fill in missing considerations to build this functional component to make it production-ready. Of course, there are tradeoffs need to be cut as the build need to happen within 3 hours.

---

REQUIRED OUTPUT

- Confirmation that agent understands the system prompt before writing spec files

DO NOT DO THE STEPS BELOW UNTIL DEVELOPER GIVE PROPER APPROVAL OR BEFORE UNDERSTANDING SYSTEM PROMPT
Generate 3 raw Markdown code blocks sequentially for:

1. `.docs/specs/requirements.md`
   - **Functional Requirements**: System API contracts, input validation, state transitions, and expected outputs.
   - **Non-Functional SLAs**: Latency limits, big-O time/space complexity targets, memory bounding, and concurrency rules.
   - **Failure & Edge Cases**: Retry policies, boundary degradation, rate-limiting rules, or eviction strategies.

2. `.docs/specs/design.md`
   - **System Architecture**: ASCII Data Flow diagram mapping client requests, HTTP handlers, memory primitives, background loops, and persistence.
   - **Primitive Mapping**: Detailed rationale for each Python data structure and synchronization primitive selected.
   - **API & Data Contracts**: Pydantic schemas, HTTP status code definitions (`200`, `202`, `400`, `404`, `429`, `500`), and persistence schemas.
   - **Concurrency & Failure Boundaries**: Locking strategies, race condition mitigations, and fail-open exception handling.

3. `.docs/specs/tasks.md`
   - A sequential steps execution checklist.
   - Each task must explicitly target files (`models.py`, `service.py`, `main.py`, `test_main.py`) and be individually testable using `pytest`.
   - Each task has sub-tasks, and each task outlines prerequisistes tasks that must be completed before tackling the current one to control topological priority of tasks.
