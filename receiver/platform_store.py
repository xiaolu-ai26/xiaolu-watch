"""Durable replacement and singleton locking on macOS and Windows.

Windows directory handles cannot be fsynced using Python's POSIX API. Every
publication instead uses MoveFileExW with WRITE_THROUGH after file fsync.
"""
import os
from pathlib import Path

if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes
    _move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
    _move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _move.restype = wintypes.BOOL
else:
    import fcntl


def replace(source, target):
    if os.name == 'nt':
        if not _move(str(Path(source).absolute()), str(Path(target).absolute()), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)


def sync_directory(path):
    if os.name == 'nt':
        # MoveFileExW WRITE_THROUGH is used for every receiver publication.
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_exclusive(handle):
    if os.name == 'nt':
        handle.seek(0)
        if not handle.read(1):
            handle.write('0')
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
