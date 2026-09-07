"""Linux Landlock filesystem boundary, installed before creating worker threads.

Only filesystem rules are used; IP egress filtering remains a separate boundary.
See https://docs.kernel.org/userspace-api/landlock.html.
"""

import ctypes
import os
from pathlib import Path
import platform
import sys

_LIBC = ctypes.CDLL(None, use_errno=True)
_READ = (1 << 0) | (1 << 2) | (1 << 3)
_HANDLED = (1 << 15) - 1  # ABI 3: includes REFER and TRUNCATE.
_WRITE = _HANDLED & ~((1 << 6) | (1 << 9) | (1 << 11))  # no devices or sockets


class _Ruleset(ctypes.Structure):
    _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def abi_version():
    if sys.platform != 'linux' or platform.machine() not in {'x86_64', 'aarch64'}:
        return 0
    return max(0, _LIBC.syscall(444, None, 0, 1))


def system_paths():
    return [Path(path) for path in (
        '/usr', '/lib', '/lib64', '/bin', '/sbin', '/etc/ssl', '/etc/pki',
        '/etc/ld.so.cache', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf',
        '/etc/gai.conf', '/etc/localtime', '/dev/urandom', '/dev/random',
        '/proc/self', '/sys/devices/system/cpu', sys.prefix, sys.base_prefix,
        str(Path(__file__).resolve().parent),
    ) if Path(path).exists()]


def restrict(read_paths=(), write_paths=()):
    abi = abi_version()
    if abi < 3:
        raise RuntimeError('Для безопасной обработки нужен Linux с Landlock ABI >= 3; проверьте ядро и seccomp контейнера.')
    attr = _Ruleset(_HANDLED, 0, 3 if abi >= 6 else 0)
    size = ctypes.sizeof(attr) if abi >= 6 else 8
    fd = _LIBC.syscall(444, ctypes.byref(attr), size, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'Cannot create worker filesystem policy')
    try:
        rules = [(path, _READ) for path in read_paths] + [(path, _WRITE) for path in write_paths]
        # Standard streams are inherited; /dev/null may also be opened by FFmpeg.
        rules.append((Path('/dev/null'), (1 << 1) | (1 << 2)))
        for path, access in rules:
            path = Path(path).resolve(strict=True)
            if not path.is_dir():
                access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
            opened = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(access, opened)
                if _LIBC.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), 'Cannot add worker filesystem rule')
            finally:
                os.close(opened)
        if _LIBC.prctl(38, 1, 0, 0, 0) != 0 or _LIBC.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot enforce worker filesystem policy')
    finally:
        os.close(fd)
