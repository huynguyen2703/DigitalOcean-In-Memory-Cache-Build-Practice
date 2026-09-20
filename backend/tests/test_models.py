import pytest
from app.models import MAX_VALUE_BYTES, CacheSetRequest
from pydantic import ValidationError


def test_valid_payload_with_ttl():
    req = CacheSetRequest(value={"a": 1}, ttl_seconds=60)
    assert req.value == {"a": 1}
    assert req.ttl_seconds == 60


def test_valid_payload_without_ttl():
    req = CacheSetRequest(value="hello")
    assert req.ttl_seconds is None


@pytest.mark.parametrize("ttl", [0, -1, -100])
def test_invalid_ttl_rejected(ttl):
    with pytest.raises(ValidationError):
        CacheSetRequest(value="x", ttl_seconds=ttl)


def test_oversized_value_rejected():
    oversized = "a" * (MAX_VALUE_BYTES + 1)
    with pytest.raises(ValidationError):
        CacheSetRequest(value=oversized)


def test_non_serializable_value_rejected():
    with pytest.raises(ValidationError):
        CacheSetRequest(value={1, 2, 3})


def test_circular_reference_rejected():
    circular: dict = {}
    circular["self"] = circular
    with pytest.raises(ValidationError):
        CacheSetRequest(value=circular)
