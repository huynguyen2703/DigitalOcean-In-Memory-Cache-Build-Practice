import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from sqlmodel import Session

from backend.app.database import create_db_and_tables, engine, get_session
from backend.app.models import CacheResponse, CacheSetRequest
from backend.app.observability import setup_observability
from backend.app.service import CacheService

SWEEP_INTERVAL_SECONDS = 30


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    create_db_and_tables()
    cache_service = CacheService()
    app.state.cache_service = cache_service

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            with Session(engine) as session:
                await cache_service.purge_expired(session)

    sweep_task = asyncio.create_task(sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task


app = FastAPI(title="DigitalOcean Prototype Service", lifespan=lifespan)
setup_observability(app)


def get_cache_service(request: Request) -> CacheService:
    return request.app.state.cache_service


# Module-level dependency/annotation singletons (FastAPI's recommended Annotated style) —
# each Depends()/Path() call happens once at import time, not per-parameter-default.
SessionDep = Annotated[Session, Depends(get_session)]
CacheServiceDep = Annotated[CacheService, Depends(get_cache_service)]
CacheKey = Annotated[str, Path(min_length=1, max_length=256)]


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.put("/cache/{key}", response_model=CacheResponse)
async def set_cache_entry(
    key: CacheKey,
    body: CacheSetRequest,
    session: SessionDep,
    cache_service: CacheServiceDep,
) -> CacheResponse:
    await cache_service.set(session, key, body.value, body.ttl_seconds)
    return CacheResponse(key=key, value=body.value)


@app.get("/cache/{key}", response_model=CacheResponse)
async def get_cache_entry(
    key: CacheKey,
    session: SessionDep,
    cache_service: CacheServiceDep,
) -> CacheResponse:
    value = await cache_service.get(session, key)
    if value is None:
        raise HTTPException(status_code=404, detail="key not found")
    return CacheResponse(key=key, value=value)


@app.delete("/cache/{key}", status_code=204)
async def delete_cache_entry(
    key: CacheKey,
    session: SessionDep,
    cache_service: CacheServiceDep,
) -> None:
    existed = await cache_service.delete(session, key)
    if not existed:
        raise HTTPException(status_code=404, detail="key not found")
