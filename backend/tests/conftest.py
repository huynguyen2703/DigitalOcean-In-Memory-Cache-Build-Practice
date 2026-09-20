import pytest
from app.database import get_session
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def client():
    # Isolated in-memory DB per test, overriding the app's real engine so integration
    # tests never touch (or depend on) the on-disk cache.db across test runs.
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(test_engine)

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:  # runs lifespan startup/shutdown
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def db_session():
    # StaticPool keeps the single in-memory connection alive for the whole test
    # instead of it (and the schema) vanishing once the first connection closes.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
