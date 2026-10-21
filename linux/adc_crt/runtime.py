"""Per-user runtime files: single-instance locks and pidfiles for the GUI and button listener.

Everything lives in ``$XDG_RUNTIME_DIR/adc-crt-control/`` (a 0700 directory on the user's
tmpfs), or ``/tmp/adc-crt-control-<uid>`` when XDG_RUNTIME_DIR is unset (e.g. a bare SSH login).

An instance holds an exclusive ``flock`` on its file for its whole lifetime and writes its pid
into it. A crashed process therefore never blocks the next one (the kernel drops the lock with
the process), and readers can tell a stale pid from a live one with pid_alive() plus
/proc/<pid>/cmdline.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from typing import Optional

APP_DIR = "adc-crt-control"
GUI_PIDFILE = "gui.pid"
BUTTONS_LOCK = "buttons.lock"
# Used when XDG_RUNTIME_DIR is unset or unusable. Looked up at call time (tests patch it).
FALLBACK_TEMPLATE = "/tmp/adc-crt-control-{uid}"


def runtime_dir(create: bool = True) -> str:
    """Return (and by default create) the private per-user runtime directory."""
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and os.path.isabs(base) and os.path.isdir(base):
        path = os.path.join(base, APP_DIR)
    else:
        path = FALLBACK_TEMPLATE.format(uid=os.getuid())
    if create:
        _ensure_private_dir(path)
    return path


def _ensure_private_dir(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)  # lstat: a symlink planted in /tmp must not be followed
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(
            f"{path} exists but is not a directory owned by uid {os.getuid()}; remove it and retry")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def instance_path(name: str) -> str:
    return os.path.join(runtime_dir(), name)


def _same_file(fd: int, path: str) -> bool:
    try:
        held = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


class InstanceLock:
    """An exclusive flock on a runtime file that also contains our pid. release() removes it."""

    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self._fd: Optional[int] = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            # Unlink while still holding the lock, and only if the path is still our file.
            if _same_file(fd, self.path):
                os.unlink(self.path)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_instance(name: str, directory: Optional[str] = None) -> Optional[InstanceLock]:
    """Lock ``<runtime dir>/<name>`` and write our pid into it.

    Returns None when another live process holds the lock. Raises OSError/RuntimeError when the
    runtime directory or file cannot be used at all.
    """
    path = os.path.join(directory if directory is not None else runtime_dir(), name)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(10):
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None
            raise
        # A previous holder may have unlinked the file between our open() and flock(); then we
        # locked an orphaned inode and must start over on the file now at `path`.
        if not _same_file(fd, path):
            os.close(fd)
            continue
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            os.close(fd)
            raise
        return InstanceLock(path, fd)
    raise RuntimeError(f"could not lock {path}: it keeps being replaced")


def read_pid(path: str) -> Optional[int]:
    """The pid stored in a pidfile, or None if missing, empty or garbage."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(64)
    except OSError:
        return None
    try:
        pid = int(data.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int, proc_root: str = "/proc") -> bool:
    """True if `pid` is a running (non-zombie) process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, belongs to another user
    except OSError:
        return False
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as handle:
            data = handle.read()
        state = data[data.rindex(b")") + 2:][:1]  # field 3, after "(comm)"
        if state in (b"Z", b"X"):
            return False
    except (OSError, ValueError):
        pass
    return True


def process_cmdline(pid: int, proc_root: str = "/proc") -> str:
    """/proc/<pid>/cmdline with NULs as spaces; "" if unreadable (gone, zombie, no /proc)."""
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    return data.replace(b"\0", b" ").decode("utf-8", "replace").strip()
