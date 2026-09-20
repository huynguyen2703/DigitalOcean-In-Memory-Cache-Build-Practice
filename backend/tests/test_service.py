import pytest
from app.repository import CacheRepository
from app.service import CacheService


class RaisingRepository(CacheRepository):
    """Stand-in repository that always fails, to exercise fail-open behavior."""

    def get(self, db, id):
        raise RuntimeError("db down")

    def upsert(self, db, *, key, value, expires_at):
        raise RuntimeError("db down")

    def delete(self, db, id, commit=True):
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_set_then_get_round_trip(db_session):
    service = CacheService(max_size=10, repository=CacheRepository())

    await service.set(db_session, "a", "1")

    assert await service.get(db_session, "a") == "1"


@pytest.mark.asyncio
async def test_get_missing_key_returns_none(db_session):
    service = CacheService(max_size=10, repository=CacheRepository())

    assert await service.get(db_session, "missing") is None


@pytest.mark.asyncio
async def test_lru_evicts_least_recently_used_key(db_session):
    service = CacheService(max_size=2, repository=CacheRepository())

    await service.set(db_session, "a", "1")
    await service.set(db_session, "b", "2")
    await service.set(db_session, "c", "3")  # capacity exceeded -> evicts "a"

    assert "a" not in service._store
    assert "b" in service._store
    assert "c" in service._store


@pytest.mark.asyncio
async def test_get_refreshes_recency_and_protects_from_eviction(db_session):
    service = CacheService(max_size=2, repository=CacheRepository())

    await service.set(db_session, "a", "1")
    await service.set(db_session, "b", "2")
    await service.get(db_session, "a")  # "a" becomes most-recently-used
    await service.set(db_session, "c", "3")  # capacity exceeded -> should evict "b", not "a"

    assert "a" in service._store
    assert "b" not in service._store
    assert "c" in service._store


@pytest.mark.asyncio
async def test_repeated_touch_preserves_correct_lru_order(db_session):
    service = CacheService(max_size=2, repository=CacheRepository())

    await service.set(db_session, "a", "1")
    await service.set(db_session, "b", "2")
    for _ in range(5):  # repeatedly move "a" to the most-recently-used end
        await service.get(db_session, "a")
    await service.set(db_session, "c", "3")  # must still evict "b" (true LRU), not "a"

    assert "a" in service._store
    assert "b" not in service._store
    assert "c" in service._store


@pytest.mark.asyncio
async def test_get_expired_entry_is_a_miss(db_session):
    service = CacheService(max_size=10, repository=CacheRepository())
    # Negative ttl_seconds puts expires_at in the past immediately (no sleep needed);
    # write-through also persists the already-expired row, so read-through can't resurrect it.
    await service.set(db_session, "a", "1", ttl_seconds=-1)

    assert await service.get(db_session, "a") is None


@pytest.mark.asyncio
async def test_purge_expired_removes_from_memory_and_db(db_session):
    repository = CacheRepository()
    service = CacheService(max_size=10, repository=repository)
    await service.set(db_session, "a", "1", ttl_seconds=-1)

    await service.purge_expired(db_session)

    assert "a" not in service._store
    assert repository.get(db_session, "a") is None


@pytest.mark.asyncio
async def test_set_is_fail_open_on_repository_error(db_session):
    service = CacheService(max_size=10, repository=RaisingRepository())

    await service.set(db_session, "a", "1")  # must not raise

    assert await service.get(db_session, "a") == "1"  # still served from memory


@pytest.mark.asyncio
async def test_get_is_fail_open_on_repository_error(db_session):
    service = CacheService(max_size=10, repository=RaisingRepository())

    assert await service.get(db_session, "missing") is None  # must not raise


@pytest.mark.asyncio
async def test_delete_removes_entry_and_returns_true(db_session):
    service = CacheService(max_size=10, repository=CacheRepository())
    await service.set(db_session, "a", "1")

    assert await service.delete(db_session, "a") is True
    assert await service.get(db_session, "a") is None


@pytest.mark.asyncio
async def test_delete_missing_key_returns_false(db_session):
    service = CacheService(max_size=10, repository=CacheRepository())

    assert await service.delete(db_session, "missing") is False
