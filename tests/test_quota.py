"""Per-user storage quota enforcement."""

import os

import pytest

from bot.utils import check_storage_quota, get_user_disk_usage, get_organized_media_path


def test_quota_allows_within_limit():
    check_storage_quota(user_id=1, incoming_bytes=1024)  # far below default 2048 MB


def test_quota_blocks_over_limit(monkeypatch):
    import config
    monkeypatch.setattr(config, 'MAX_USER_STORAGE_MB', 1)  # 1 MB quota

    folder, _rel = get_organized_media_path(1, 'photo')
    path = os.path.join(folder, 'big.bin')
    with open(path, 'wb') as f:
        f.write(b'\0' * (1024 * 1024))  # exactly 1 MB used
    try:
        assert get_user_disk_usage(1) >= 1024 * 1024
        with pytest.raises(ValueError, match='quota'):
            check_storage_quota(user_id=1, incoming_bytes=1024)
    finally:
        os.remove(path)


def test_quota_disabled_when_zero(monkeypatch):
    import config
    monkeypatch.setattr(config, 'MAX_USER_STORAGE_MB', 0)
    check_storage_quota(user_id=1, incoming_bytes=10**12)  # no limit
