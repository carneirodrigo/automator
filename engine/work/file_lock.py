"""Advisory file locking helper for coordinating concurrent engine runs.

Uses POSIX ``fcntl.flock`` on Linux/macOS and degrades to a no-op on
platforms that do not provide it (primarily Windows). A sidecar lock file
(``<path>.lock``) is used so the lock survives atomic ``os.replace`` calls
on the data file itself.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

try:
    import fcntl as _fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]


class LockUnavailable(RuntimeError):
    """Raised when a non-blocking lock attempt finds the lock already held."""


@contextlib.contextmanager
def locked(
    path: Path,
    *,
    exclusive: bool = True,
    non_blocking: bool = False,
) -> Iterator[None]:
    """Hold an advisory lock on ``<path>.lock`` for the scope of the block.

    - ``exclusive=True`` (default) → LOCK_EX; concurrent engines block.
    - ``exclusive=False`` → LOCK_SH; multiple readers may hold the lock.
    - ``non_blocking=True`` → try once; raise ``LockUnavailable`` on contention.

    The lock file sits next to the target and is created lazily. If the
    platform lacks ``fcntl`` the helper becomes a no-op so callers do not
    need to branch.
    """
    if _fcntl is None:
        yield
        return

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        mode = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        if non_blocking:
            mode |= _fcntl.LOCK_NB
        try:
            _fcntl.flock(handle.fileno(), mode)
        except BlockingIOError as exc:
            raise LockUnavailable(
                f"Lock already held on {path} — another engine run is in progress."
            ) from exc
        try:
            yield
        finally:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
