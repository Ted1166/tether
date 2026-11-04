import pytest

from app.schemas.policy import ContactPolicy, Policy


@pytest.fixture
def policy() -> Policy:
    return Policy(
        contacts=ContactPolicy(allowed=frozenset({"dan@example.com", "maya@example.com"})),
    )
