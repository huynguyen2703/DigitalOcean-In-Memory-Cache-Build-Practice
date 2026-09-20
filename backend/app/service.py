import asyncio
import json
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session

from app.repository import CacheRepository

logger = logging.getLogger("app.service")

DEFAULT_MAX_SIZE = 10000


def _utcnow() -> datetime:
    # Naive UTC: SQLite strips tzinfo on round-trip (see repository.py), so all timestamps
    # compared against DB-loaded values must stay naive-UTC throughout.
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class CacheEntry:
    value: Any
    expires_at: datetime | None


class CacheService:
    """In-memory LRU cache with SQLite fail-open fallback (design.md §2-3).

    Recency + storage are unified in a single `OrderedDict`: `move_to_end` and `popitem(last=False)`
    are genuine O(1) operations (pointer relinking in CPython's underlying linked-hash-map), so there
    is no separate recency queue and no stale-entry bookkeeping to reconcile or clean up.
    """

    def __init__(
        self,
        max_size: int | None = None,
        repository: CacheRepository | None = None,
    ) -> None:
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_size = max_size or int(os.environ.get("CACHE_MAX_SIZE", DEFAULT_MAX_SIZE))
        self._repository = repository or CacheRepository()

    def _evict_if_needed(self, incoming_key: str) -> None:
        # Overwriting an existing key never grows the store, so only new keys can trigger eviction.
        if incoming_key in self._store:
            return
        while len(self._store) >= self._max_size:
            self._store.popitem(last=False)  # evict the true LRU key (head of order)

    @staticmethod
    def _is_expired(entry: CacheEntry, now: datetime) -> bool:
        return entry.expires_at is not None and entry.expires_at <= now

    async def get(self, session: Session, key: str) -> Any | None:
        async with self._lock:
            now = _utcnow()
            entry = self._store.get(key)
            if entry is not None:
                if self._is_expired(entry, now):
                    del self._store[key]
                    logger.debug("cache entry expired key=%s", key)
                else:
                    self._store.move_to_end(key)
                    return entry.value

            # In-memory miss (absent or just-expired): best-effort read-through from SQLite.
            try:
                record = self._repository.get(session, key)
                if record is None or (record.expires_at is not None and record.expires_at <= now):
                    return None
                value = json.loads(record.value)
            except Exception:
                logger.exception("read-through failed key=%s; treating as miss", key)
                return None

            self._evict_if_needed(key)
            self._store[key] = CacheEntry(value=value, expires_at=record.expires_at)
            return value

    async def set(
        self, session: Session, key: str, value: Any, ttl_seconds: int | None = None
    ) -> None:
        async with self._lock:
            now = _utcnow()
            expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None

            self._evict_if_needed(key)
            # Assigning to an existing OrderedDict key updates the value in place but does NOT
            # move it to the end, so an explicit move_to_end is required to mark it as freshest.
            self._store[key] = CacheEntry(value=value, expires_at=expires_at)
            self._store.move_to_end(key)

            try:
                self._repository.upsert(
                    session, key=key, value=json.dumps(value), expires_at=expires_at
                )
            except Exception:
                logger.exception("write-through failed key=%s", key)

    async def delete(self, session: Session, key: str) -> bool:
        async with self._lock:
            existed_in_memory = key in self._store
            if existed_in_memory:
                del self._store[key]

            try:
                existed_in_db = self._repository.delete(session, key)
            except Exception:
                logger.exception("delete write-through failed key=%s", key)
                existed_in_db = False

            # DB is a fallback source of truth (get() read-through), so a key living only
            # in SQLite must still be reported/treated as an existing delete, not a 404.
            return existed_in_memory or existed_in_db

    async def purge_expired(self, session: Session) -> None:
        async with self._lock:
            now = _utcnow()
            expired_keys = [k for k, e in self._store.items() if self._is_expired(e, now)]
            for key in expired_keys:
                del self._store[key]
                try:
                    self._repository.delete(session, key)
                except Exception:
                    logger.exception("purge delete failed key=%s", key)
