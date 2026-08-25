"""Shared pytest fixtures.

The unit suite never touches the network. Live-API checks live in
``test_integration.py`` behind the ``integration`` marker, which
``pyproject.toml`` deselects by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def fixture_bytes():
    """Read a fixture file as bytes."""

    def _read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return _read


@pytest.fixture
def fixture_text():
    """Read a fixture file as text."""

    def _read(name: str, encoding: str = "utf-8") -> str:
        return (FIXTURES / name).read_text(encoding=encoding)

    return _read


@pytest.fixture
def mapper():
    from gwaspoker.mapping.mapper import get_mapper

    return get_mapper()


@pytest.fixture
def config():
    from gwaspoker.config import GWASPokerConfig

    return GWASPokerConfig()


@pytest.fixture(autouse=True)
def _clear_failures():
    """Keep the process-wide failure log from leaking between tests."""
    from gwaspoker.failures import FAILURES

    FAILURES._records.clear()
    yield
    FAILURES._records.clear()
