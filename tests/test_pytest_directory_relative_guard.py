"""Linux directory-descriptor writes must be checked at their actual location."""
import os
import sys

import pytest

from tests.conftest import (
    PytestPersistenceIsolationError,
    _directory_relative_path,
    _reject_protected_write,
)


@pytest.mark.skipif(sys.platform != "linux", reason="descriptor resolution requires Linux /proc")
def test_relative_directory_descriptor_is_not_resolved_against_checkout(tmp_path):
    (tmp_path / "config").mkdir()
    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        assert _directory_relative_path("config", descriptor) == tmp_path / "config"
        # Same basename as the protected repository config directory is safe.
        os.rmdir("config", dir_fd=descriptor)
    finally:
        os.close(descriptor)
    assert not (tmp_path / "config").exists()


def test_unresolvable_directory_descriptor_fails_closed():
    with pytest.raises(PytestPersistenceIsolationError, match="descriptor"):
        _directory_relative_path("config", -999)


@pytest.mark.skipif(sys.platform != "linux", reason="descriptor resolution requires Linux /proc")
def test_descriptor_to_protected_root_remains_blocked():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    descriptor = os.open(root, os.O_RDONLY)
    try:
        with pytest.raises(PytestPersistenceIsolationError, match="persistent state"):
            _reject_protected_write(_directory_relative_path("config", descriptor))
    finally:
        os.close(descriptor)
