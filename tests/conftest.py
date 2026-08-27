"""Test fixtures: isolated DATA_DIR and a fresh database per test.

DATA_DIR must be set before any bot module is imported, because config.py
resolves paths at import time - pytest imports conftest first, so this works.
"""

import os
import sys
import tempfile

import pytest

TEST_DATA_DIR = tempfile.mkdtemp(prefix="bottest_")
os.environ['DATA_DIR'] = TEST_DATA_DIR
os.environ.setdefault('MAX_USER_STORAGE_MB', '2048')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_PATH  # noqa: E402
from bot.database import init_database  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Give every test a brand-new database."""
    for suffix in ('', '-wal', '-shm'):
        path = DATABASE_PATH + suffix
        if os.path.exists(path):
            os.remove(path)
    init_database()
    yield


class FakeUser:
    """Minimal stand-in for telegram.User in builder tests."""
    def __init__(self, user_id=42, first_name="Test_*user*"):
        self.id = user_id
        self.first_name = first_name


@pytest.fixture
def user():
    return FakeUser()
