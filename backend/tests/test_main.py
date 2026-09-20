from app.main import app
from app.service import CacheService


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_set_then_get_round_trip(client):
    put_resp = client.put("/cache/greeting", json={"value": "hello"})
    assert put_resp.status_code == 200
    assert put_resp.json() == {"key": "greeting", "value": "hello"}

    get_resp = client.get("/cache/greeting")
    assert get_resp.status_code == 200
    assert get_resp.json() == {"key": "greeting", "value": "hello"}


def test_get_unknown_key_returns_404(client):
    response = client.get("/cache/does-not-exist")
    assert response.status_code == 404


def test_oversized_value_rejected(client):
    oversized = "a" * (64 * 1024 + 1)
    response = client.put("/cache/big", json={"value": oversized})
    assert response.status_code == 422


def test_invalid_ttl_rejected(client):
    response = client.put("/cache/bad-ttl", json={"value": "x", "ttl_seconds": 0})
    assert response.status_code == 422


def test_oversized_key_rejected(client):
    response = client.put(f"/cache/{'k' * 257}", json={"value": "x"})
    assert response.status_code == 422


def test_delete_existing_key_then_get_404(client):
    client.put("/cache/temp", json={"value": "gone-soon"})

    delete_resp = client.delete("/cache/temp")
    assert delete_resp.status_code == 204

    get_resp = client.get("/cache/temp")
    assert get_resp.status_code == 404


def test_delete_missing_key_returns_404(client):
    response = client.delete("/cache/never-existed")
    assert response.status_code == 404


def test_set_beyond_capacity_evicts_lru_key(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.database import get_session

    monkeypatch.setenv("CACHE_MAX_SIZE", "2")
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(test_engine)

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        with TestClient(app) as small_client:
            small_client.put("/cache/a", json={"value": "1"})
            small_client.put("/cache/b", json={"value": "2"})
            small_client.put("/cache/c", json={"value": "3"})  # capacity exceeded -> evicts "a"

            assert app.state.cache_service._max_size == 2
            assert "a" not in app.state.cache_service._store
            assert "b" in app.state.cache_service._store
            assert "c" in app.state.cache_service._store
    finally:
        app.dependency_overrides.clear()


def test_restart_read_through_repopulates_cache(client):
    put_resp = client.put("/cache/durable", json={"value": "persisted"})
    assert put_resp.status_code == 200

    # Simulate a cold restart: replace the in-memory cache but keep the same (overridden)
    # DB session dependency, proving GET repopulates from SQLite (requirements.md §1.4).
    app.state.cache_service = CacheService()
    assert "durable" not in app.state.cache_service._store

    get_resp = client.get("/cache/durable")
    assert get_resp.status_code == 200
    assert get_resp.json() == {"key": "durable", "value": "persisted"}
