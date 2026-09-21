"""Nonblocking process locks for cooperative executions using one state path.

Keep the context open from the first state read through publication and Git.
The persistent sibling lock file is never unlinked: replacing or deleting it
would let another process lock a different inode. These are local OS advisory
locks, not coordination between hosts or protection from noncooperating writers.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


class StateLockError(RuntimeError):
    """Execution cannot safely proceed without its state lock."""


class StateLockBusy(StateLockError):
    """Another execution currently owns the same state lock."""


_PLATFORM = os.name


def _lock_operations():
    try:
        if _PLATFORM == "nt":
            import msvcrt

            def acquire(fd):
                os.lseek(fd, 0, os.SEEK_SET)
                # Python documents byte locks beyond EOF; no shared writes or
                # PID records are needed to lock the initially empty file.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def release(fd):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

            return acquire, release
        if _PLATFORM == "posix":
            import fcntl

            return (lambda fd: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB),
                    lambda fd: fcntl.flock(fd, fcntl.LOCK_UN))
    except ImportError:
        pass
    raise StateLockError("State execution locking is unsupported on this platform")


def _canonical_state_path(state_path):
    try:
        raw = os.fspath(state_path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError
        path = Path(os.path.normcase(os.path.realpath(os.path.abspath(raw))))
        if not path.name or path.is_dir():
            raise ValueError
        return path
    except (OSError, TypeError, ValueError):
        raise StateLockError("Invalid state path for execution locking") from None


def _open_lock_file(lock_path):
    fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise ValueError
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError
        return fd
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        raise StateLockError("Unable to open the state execution lock") from None


def _assert_same_file(fd, lock_path):
    try:
        opened = os.fstat(fd)
        current = os.stat(lock_path, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError
    except (OSError, ValueError):
        raise StateLockError("State execution lock file changed during acquisition") from None


@contextmanager
def state_execution_lock(state_path):
    """Yield the canonical state Path while holding its exclusive process lock.

    Acquisition never waits or steals a purportedly stale lock. Contention raises
    StateLockBusy; unsupported platforms and I/O failures raise StateLockError.
    Error messages contain neither the supplied path nor the OS exception text.
    Normal context exit and process termination release the OS lock; the empty
    sibling ``.<state filename>.execution.lock`` remains for later executions.
    """
    canonical = _canonical_state_path(state_path)
    acquire, release = _lock_operations()
    lock_path = canonical.with_name(f".{canonical.name}.execution.lock")
    fd = _open_lock_file(lock_path)
    acquired = False
    try:
        try:
            acquire(fd)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise StateLockBusy("Another execution already holds this state lock") from None
            raise StateLockError("Unable to acquire the state execution lock") from None
        acquired = True
        _assert_same_file(fd, lock_path)
        yield canonical
    finally:
        release_failed = False
        if acquired:
            try:
                release(fd)
            except OSError:
                release_failed = True
        try:
            os.close(fd)
        except OSError:
            release_failed = True
        if release_failed:
            raise StateLockError("Unable to release the state execution lock cleanly") from None
