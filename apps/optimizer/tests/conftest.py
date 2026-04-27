"""Pytest fixtures shared across the optimizer test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.state import firestore as fs

from tests.fake_firestore import FakeFirestore


@pytest.fixture
def fake_db() -> Iterator[FakeFirestore]:
    """Provide a fresh in-memory Firestore for each test."""
    db = FakeFirestore()
    fs.set_client_for_testing(db)
    try:
        yield db
    finally:
        fs.set_client_for_testing(None)
