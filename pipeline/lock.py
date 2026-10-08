"""Cross-process exclusive lock (one render at a time on weak hardware).

Uses an OS-level byte-range lock, so it is released automatically if the process dies.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

from .config import path

if os.name == "nt":
    import msvcrt

    def _try_lock(fd) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def single_instance(name: str):
    """Yield True while this process holds `name`, or False at once if another process holds it."""
    fd = os.open(path("cache") / f"{name}.lock", os.O_RDWR | os.O_CREAT)
    try:
        mine = _try_lock(fd)
        try:
            yield mine
        finally:
            if mine:
                _unlock(fd)
    finally:
        os.close(fd)


@contextmanager
def exclusive(name: str, wait_seconds: float = 3 * 3600, on_wait=None):
    f = path("cache") / f"{name}.lock"
    fd = os.open(f, os.O_RDWR | os.O_CREAT)
    try:
        start, told = time.time(), False
        while not _try_lock(fd):
            if not told and on_wait:
                on_wait()
                told = True
            if time.time() - start > wait_seconds:
                raise TimeoutError(f"lock {name} still held after {wait_seconds}s")
            time.sleep(2)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
