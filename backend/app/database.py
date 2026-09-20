import logging
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger("app.database")

DATABASE_URL = "sqlite:///./cache.db"

# check_same_thread=False: FastAPI may service requests on a different thread than the one
# that created the engine; safe here since each request gets its own short-lived Session.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    # SQLite is a secondary store (requirements.md §2.4): a broken/unwritable DB file must not
    # prevent the process from booting and serving an in-memory-only cache.
    try:
        SQLModel.metadata.create_all(engine)
    except Exception:
        logger.exception("Failed to initialize SQLite schema; continuing in-memory-only")


def get_session() -> Generator[Session, None, None]:
    session = Session(engine)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
