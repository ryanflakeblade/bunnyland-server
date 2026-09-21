"""Terminal file persistence with POSIX ownership checks or inherited Windows ACLs.

Windows does not expose POSIX uid ownership or implement owner-only chmod modes.
Access there is governed by the containing directory's Windows ACL; callers should
use a private user profile directory. Regular-file and symlink checks apply on both.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from uuid import uuid4


def _require_owned(metadata: os.stat_result, path: Path) -> None:
    if sys.platform == "win32":
        return
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"path is not owned by the current user: {path}")


def secure_directory(path: Path) -> None:
    """Create or tighten one user-owned directory without following a symlink."""

    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        if cursor.is_symlink():
            raise PermissionError(f"path must be a regular directory: {cursor}")
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=False)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PermissionError(f"could not create secure directory: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError(f"path must be a regular directory: {path}")
    _require_owned(metadata, path)
    path.chmod(0o700)


def _safe_existing_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError(f"path must be a regular non-symlink file: {path}")
    _require_owned(metadata, path)
    path.chmod(0o600)
    return metadata


def secure_read_text(path: Path) -> str:
    """Read an existing owner-only regular file without following a symlink."""

    _safe_existing_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"path must be a regular file: {path}")
        _require_owned(metadata, path)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            return source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def secure_write_text(path: Path, contents: str) -> None:
    """Atomically replace an owner-only file using a no-follow temporary file."""

    secure_directory(path.parent)
    try:
        _safe_existing_file(path)
    except FileNotFoundError:
        pass
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            descriptor = -1
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = ["secure_directory", "secure_read_text", "secure_write_text"]
