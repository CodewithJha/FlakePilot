"""A short write is not a full store.

The runner probe printed `disk_errno=0` after one `write` of 2 MB. That call
can return a short count without raising. This test drives the probe function.
It does not start a container and it does not pass an isolation check.
"""

import importlib.util
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "ops" / "isolation" / "probe.py"


class _Writer(Protocol):
    def write(self, data: bytes) -> int: ...


class _Fill(Protocol):
    def __call__(
        self, handle: _Writer, chunk: bytes, target: int
    ) -> tuple[int, int]: ...


class _StopsAt:
    """Accept one byte at a time, then raise ENOSPC."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stored = 0

    def write(self, data: bytes) -> int:
        if self.stored >= self.limit:
            raise OSError(28, "enospc")
        self.stored += 1
        return 1 if data else 0


class _Accepts:
    def write(self, data: bytes) -> int:
        return len(data)


class _Zero:
    def write(self, data: bytes) -> int:
        del data
        return 0


def _fill() -> _Fill:
    spec = importlib.util.spec_from_file_location("isolation_probe", PROBE)
    if spec is None or spec.loader is None:
        raise RuntimeError("probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_Fill, vars(module)["fill_until"])


def test_a_short_write_is_continued_until_the_filesystem_refuses() -> None:
    written, errno = _fill()(_StopsAt(4), b"x" * 64, 64)
    assert written == 4
    assert errno == 28


def test_a_zero_count_is_not_success() -> None:
    written, errno = _fill()(_Zero(), b"x" * 64, 64)
    assert written == 0
    assert errno == 28


def test_a_full_store_is_the_only_zero_errno() -> None:
    written, errno = _fill()(_Accepts(), b"x" * 8, 24)
    assert written == 24
    assert errno == 0
