"""Shared pytest fixtures for claude-memory tests."""

import os
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest


@pytest.fixture
async def db_pool():
    """
    Connection pool for a local test database.

    Default targets the slmbeast test container at localhost:5434. Override via
    TEST_DATABASE_URL when running from elsewhere.
    """
    url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@localhost:5434/claude_memory_test",
    )
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture
def mock_openai():
    """Mock OpenAI client that returns a fixed embedding."""
    client = MagicMock()
    client.embeddings = MagicMock()
    client.embeddings.create = AsyncMock()
    client.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.1] * 1536)]
    )
    return client


@pytest.fixture
def mock_anthropic():
    """Mock Anthropic client for judge testing."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client
