from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from nerya.core.errors import ProtectedScopeViolation
from nerya.evolution import promotion


pytestmark = pytest.mark.smoke


@pytest.fixture
def staged_os(monkeypatch):
    opened = []

    def track_open(*args, **kwargs):
        fd = os.open(*args, **kwargs)
        opened.append(fd)
        return fd

    boundary = SimpleNamespace(
        O_RDONLY=os.O_RDONLY, O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 0),
        open=track_open, fstat=os.fstat, fdopen=os.fdopen, close=os.close,
        opened=opened,
    )
    monkeypatch.setattr(promotion, "os", boundary)
    return boundary


def assert_closed(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF
    else:
        os.close(fd)
        pytest.fail("staged file descriptor leaked")


def test_read_failure_does_not_close_reused_fd(tmp_path, staged_os):
    staged = tmp_path / "candidate.txt"
    staged.write_bytes(b"candidate")
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_bytes(b"still open")
    survivor = os.open(unrelated, os.O_RDONLY)
    reused = []
    failure = OSError(errno.EIO, "injected read failure")

    def fail_read():
        raise failure

    @contextmanager
    def failing_reader(fd, mode):
        try:
            with os.fdopen(fd, mode) as handle:
                yield SimpleNamespace(read=fail_read, fileno=handle.fileno)
        finally:
            # Reproduce another opener reusing the descriptor after the file
            # object closes it, before the caller's exception handler runs.
            os.dup2(survivor, fd)
            reused.append(fd)

    staged_os.fdopen = failing_reader
    try:
        with pytest.raises(OSError) as caught:
            promotion._read_staged_file(staged)
        assert caught.value is failure
        assert len(reused) == 1
        assert os.read(reused[0], 10) == b"still open"
    finally:
        for fd in [*reused, survivor]:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


@pytest.mark.parametrize("phase", ["fstat", "fdopen"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_setup_failure_closes_owned_fd(tmp_path, staged_os, phase, error_type):
    staged = tmp_path / "candidate.txt"
    staged.write_bytes(b"candidate")
    failure = error_type("injected setup failure")

    def fail(*args, **kwargs):
        raise failure

    setattr(staged_os, phase, fail)
    with pytest.raises(error_type) as caught:
        promotion._read_staged_file(staged)
    assert caught.value is failure
    assert len(staged_os.opened) == 1
    assert_closed(staged_os.opened[0])


def test_non_regular_file_is_rejected_and_closed(tmp_path, staged_os):
    with pytest.raises(ProtectedScopeViolation, match="not a regular file"):
        promotion._read_staged_file(tmp_path)
    assert len(staged_os.opened) == 1
    assert_closed(staged_os.opened[0])


def test_success_preserves_bytes_and_mode_and_closes_fd(tmp_path, staged_os):
    staged = tmp_path / "candidate.txt"
    staged.write_bytes(b"candidate\x00\xff")
    staged.chmod(0o640)

    assert promotion._read_staged_file(staged) == (b"candidate\x00\xff", 0o640)
    assert len(staged_os.opened) == 1
    assert_closed(staged_os.opened[0])
