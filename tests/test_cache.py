"""Cache freshness helpers: stale() and the offline-first refresh_if_stale()."""

import os
from datetime import timedelta

import pytest

from travelplanner.roads import refresh_if_stale, stale


def _write(path, text="x"):
    path.write_text(text, encoding="utf-8")


def _age(path, seconds):
    """Backdate a file's mtime by `seconds` so it reads as that old."""
    past = os.path.getmtime(path) - seconds
    os.utime(path, (past, past))


def test_stale_when_missing(tmp_path):
    assert stale(str(tmp_path / "absent"), timedelta(days=1)) is True


def test_fresh_file_is_not_stale(tmp_path):
    p = tmp_path / "f"
    _write(p)
    assert stale(str(p), timedelta(days=1)) is False


def test_old_file_is_stale(tmp_path):
    p = tmp_path / "f"
    _write(p)
    _age(p, timedelta(days=2).total_seconds())
    assert stale(str(p), timedelta(days=1)) is True


def test_max_age_none_never_stale(tmp_path):
    p = tmp_path / "f"
    _write(p)
    _age(p, timedelta(days=3650).total_seconds())
    assert stale(str(p), None) is False        # cache forever once present


def test_refresh_downloads_when_missing(tmp_path):
    p = tmp_path / "f"
    calls = []
    did = refresh_if_stale(str(p), timedelta(days=1),
                           lambda: (calls.append(1), _write(p)), label="t")
    assert did is True and calls == [1]


def test_refresh_skips_when_fresh(tmp_path):
    p = tmp_path / "f"
    _write(p)
    calls = []
    did = refresh_if_stale(str(p), timedelta(days=1),
                           lambda: calls.append(1), label="t")
    assert did is False and calls == []        # fresh: no download


def test_refresh_redownloads_when_stale(tmp_path):
    p = tmp_path / "f"
    _write(p, "old")
    _age(p, timedelta(days=2).total_seconds())
    did = refresh_if_stale(str(p), timedelta(days=1),
                           lambda: _write(p, "new"), label="t")
    assert did is True and p.read_text() == "new"


def test_refresh_failure_keeps_stale_copy_and_warns(tmp_path):
    p = tmp_path / "f"
    _write(p, "cached")
    _age(p, timedelta(days=2).total_seconds())

    def boom():
        raise OSError("offline")

    with pytest.warns(UserWarning, match="refresh failed"):
        did = refresh_if_stale(str(p), timedelta(days=1), boom, label="t")
    assert did is False and p.read_text() == "cached"   # offline-first: kept


def test_refresh_failure_with_no_cache_raises(tmp_path):
    p = tmp_path / "absent"

    def boom():
        raise OSError("offline")

    with pytest.raises(OSError):
        refresh_if_stale(str(p), timedelta(days=1), boom, label="t")
