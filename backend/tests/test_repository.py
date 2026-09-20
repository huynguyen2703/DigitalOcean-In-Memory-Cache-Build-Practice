from datetime import datetime, timedelta, timezone

from app.repository import CacheRepository

repo = CacheRepository()


def test_upsert_creates_new_record(db_session):
    record = repo.upsert(db_session, key="a", value="1", expires_at=None)
    assert record.key == "a"
    assert record.value == "1"
    assert repo.get(db_session, "a") is not None


def test_upsert_updates_existing_record(db_session):
    repo.upsert(db_session, key="a", value="1", expires_at=None)
    updated = repo.upsert(db_session, key="a", value="2", expires_at=None)

    assert updated.value == "2"
    refreshed = repo.get(db_session, "a")
    assert refreshed is not None
    assert refreshed.value == "2"
    assert len(repo.list(db_session)) == 1  # still one row, not a duplicate insert


def test_upsert_sets_expiry(db_session):
    # Naive UTC: SQLite drops tzinfo on round-trip, so store/compare naive throughout.
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)
    record = repo.upsert(db_session, key="a", value="1", expires_at=expires)
    assert record.expires_at == expires


def test_get_missing_key_returns_none(db_session):
    assert repo.get(db_session, "missing") is None


def test_delete_missing_key_returns_false(db_session):
    assert repo.delete(db_session, "missing") is False


def test_delete_existing_key_returns_true(db_session):
    repo.upsert(db_session, key="a", value="1", expires_at=None)

    assert repo.delete(db_session, "a") is True
    assert repo.get(db_session, "a") is None
