import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, field_validator
from sqlmodel import Field, SQLModel

MAX_KEY_LENGTH = 256
MAX_VALUE_BYTES = 64 * 1024


class CacheRecord(SQLModel, table=True):
    """SQLite-persisted representation of a cache entry (secondary store)."""

    key: str = Field(primary_key=True, max_length=MAX_KEY_LENGTH)
    value: str
    expires_at: datetime | None = None
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class CacheSetRequest(BaseModel):
    value: Any
    ttl_seconds: int | None = None

    @field_validator("ttl_seconds")
    @classmethod
    def validate_ttl(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        return v

    @field_validator("value")
    @classmethod
    def validate_value_size(cls, v: Any) -> Any:
        # Serialized (not raw in-memory) size is what actually bounds SQLite/dict storage.
        # RecursionError catches pathologically deep (non-circular) nesting; TypeError/ValueError
        # catch non-serializable objects and circular references — none of these should ever
        # escape as an unhandled 500, they must degrade to a clean 400/422 validation error.
        try:
            serialized = json.dumps(v)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("value must be JSON-serializable") from exc
        if len(serialized.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValueError(f"value exceeds max serialized size of {MAX_VALUE_BYTES} bytes")
        return v


class CacheResponse(BaseModel):
    key: str
    value: Any
