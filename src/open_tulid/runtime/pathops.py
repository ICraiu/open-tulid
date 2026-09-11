"""Containment-safe filesystem operations for source transport (reliability R3).

File/directory replacement across baseline capture, candidate seal, promotion,
and recovery must never follow an attacker/worker-swapped symlink outside the
admitted root. Path-string containment checks (``root in path.parents``) race
with path resolution and ``shutil.copy2(...)``/``Path.unlink()`` follow
symlinks, so a regular file or parent directory swapped to a link between a
scan and a copy can read or mutate external bytes.

These helpers operate on directory file descriptors with ``O_NOFOLLOW``:
an intermediate directory and the final regular file are re-opened at use time,
which closes the check-then-copy window on Linux (the target platform). A parent
directory swapped to a link, or a checked regular file swapped to a link, is
rejected by the open rather than dereferenced.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Sequence

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NOCTTY = getattr(os, "O_NOCTTY", 0)


class SourcePathError(OSError):
    """A source path changed type (or became a link) between check and use."""


def _flags(*bits: int) -> int:
    value = 0
    for bit in bits:
        value |= bit
    return value


def open_root(root: Path) -> int:
    """Open the admitted root as a directory fd.

    ``root`` is the admitted container: a path the operator configured, never a
    path an attacker/worker swapped. It is opened as-is with ``O_NOFOLLOW |
    O_DIRECTORY`` (no ``resolve()`` first) so a root itself swapped to a link
    pointing outside the admitted area is rejected rather than dereferenced.
    Every relative component beneath it is also opened with ``O_NOFOLLOW``.
    """
    try:
        return os.open(str(root), _flags(os.O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC))
    except OSError as exc:
        if exc.errno in (getattr(os, "ENOTDIR", 0), getattr(os, "ELOOP", 0)):
            raise SourcePathError(f"admitted root is not a real directory (symlink?): {root}")
        raise


def relative_parts(relative: str) -> tuple[str, ...]:
    """Split a repository-relative path, rejecting absolute or escaping parts.

    The returned parts are names only; containment is enforced later by
    ``O_NOFOLLOW`` directory-descriptor opens, never by re-resolving the string.
    """
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.parts == ():
        raise ValueError(f"unsafe relative path: {relative!r}")
    return path.parts


def open_parent_nofollow(root_fd: int, parent_parts: Sequence[str]) -> int:
    """Open the parent directory of a final component beneath ``root_fd``.

    Each relative part is opened with ``O_NOFOLLOW | O_DIRECTORY`` from the
    previous fd, so an intermediate directory swapped to a link pointing outside
    the admitted root is rejected at use time. The returned fd is independent
    (the caller closes it); when there are no intermediate parts it is a dup of
    the root fd.
    """
    if not parent_parts:
        return os.dup(root_fd)
    fd = root_fd
    try:
        for part in parent_parts:
            child = os.open(
                part,
                _flags(os.O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC),
                dir_fd=fd,
            )
            if fd != root_fd:
                os.close(fd)
            fd = child
        return fd
    except BaseException:
        if fd != root_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def open_target_parents(root_fd: int, parent_parts: Sequence[str]) -> int:
    """Open (creating if needed) parent directories beneath ``root_fd``.

    Created directories are opened with ``O_NOFOLLOW`` so a swapped parent link
    is rejected. A parent that exists as a non-directory (e.g. a file being
    replaced by a directory) is a conflict and raises.
    """
    if not parent_parts:
        return os.dup(root_fd)
    fd = root_fd
    try:
        for part in parent_parts:
            try:
                child = os.open(
                    part,
                    _flags(os.O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC),
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.mkdir(part, dir_fd=fd)
                child = os.open(
                    part,
                    _flags(os.O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC),
                    dir_fd=fd,
                )
            if fd != root_fd:
                os.close(fd)
            fd = child
        return fd
    except BaseException:
        if fd != root_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def copy_regular_nofollow(
    *,
    source_root: Path,
    source_relative: str,
    target_root: Path,
    target_relative: str,
    mode: int | None = None,
) -> None:
    """Copy a regular file across roots without following any symlink.

    The source is opened with ``O_NOFOLLOW`` from a ``O_NOFOLLOW``-walked parent
    fd, so a checked regular file (or its parent directory) swapped to a link
    pointing outside ``source_root`` is rejected instead of dereferenced; the
    external sentinel is never read into accepted evidence. The byte read is
    bound to the opened inode, closing the check-then-copy window.
    """
    src_parts = relative_parts(source_relative)
    tgt_parts = relative_parts(target_relative)
    src_root_fd = open_root(source_root)
    try:
        tgt_root_fd = open_root(target_root)
    except FileNotFoundError:
        # The admitted target root may not yet exist; create it (never any
        # symlink-bearing component we invent ourselves).
        target_root.mkdir(parents=True)
        tgt_root_fd = open_root(target_root)
    src_parent = tgt_parent = None
    src_fd = dst_fd = -1
    try:
        src_parent = open_parent_nofollow(src_root_fd, src_parts[:-1])
        src_fd = os.open(
            src_parts[-1],
            _flags(os.O_RDONLY, O_NONBLOCK, O_NOFOLLOW, O_CLOEXEC),
            dir_fd=src_parent,
        )
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise SourcePathError(
                f"Unsupported source filesystem entry (expected a regular file): {source_relative}"
            )
        effective_mode = (st.st_mode & 0o777) if mode is None else mode

        tgt_parent = open_target_parents(tgt_root_fd, tgt_parts[:-1])
        dst_fd = os.open(
            tgt_parts[-1],
            _flags(os.O_WRONLY, os.O_CREAT, os.O_TRUNC, O_NOFOLLOW, O_CLOEXEC),
            dir_fd=tgt_parent,
            mode=effective_mode,
        )
        os.fchmod(dst_fd, effective_mode)
        _copy_fd_bytes(src_fd, dst_fd)
    finally:
        for fd in (src_fd, dst_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for parent in (src_parent, tgt_parent):
            if parent is not None:
                try:
                    os.close(parent)
                except OSError:
                    pass
        os.close(src_root_fd)
        os.close(tgt_root_fd)


def hash_regular_nofollow(root: Path, relative: str, *, block_size: int = 1048576) -> str:
    """SHA-256 of a regular file opened with ``O_NOFOLLOW`` beneath ``root``.

    Binds the identity to an opened inode and rejects a file swapped to a link.
    """
    import hashlib

    parts = relative_parts(relative)
    root_fd = open_root(root)
    parent = None
    src_fd = -1
    try:
        parent = open_parent_nofollow(root_fd, parts[:-1])
        src_fd = os.open(
            parts[-1],
            _flags(os.O_RDONLY, O_NONBLOCK, O_NOFOLLOW, O_CLOEXEC),
            dir_fd=parent,
        )
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise SourcePathError(
                f"Unsupported source filesystem entry (expected a regular file): {relative}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(src_fd, block_size)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        if src_fd >= 0:
            try:
                os.close(src_fd)
            except OSError:
                pass
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass
        os.close(root_fd)


def regular_file_id(root: Path, relative: str, *, block_size: int = 1048576) -> tuple[str, int, int]:
    """Open a regular file beneath ``root`` and return (sha256, size_bits, mode).

    The open uses ``O_NOFOLLOW`` and binds the digest to the opened inode, so a
    file swapped to a link between a scan and hashing is rejected instead of
    swallowing external bytes into an accepted manifest.
    """
    import hashlib

    parts = relative_parts(relative)
    root_fd = open_root(root)
    parent = None
    src_fd = -1
    try:
        parent = open_parent_nofollow(root_fd, parts[:-1])
        src_fd = os.open(
            parts[-1],
            _flags(os.O_RDONLY, O_NONBLOCK, O_NOFOLLOW, O_CLOEXEC),
            dir_fd=parent,
        )
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise SourcePathError(
                f"Unsupported source filesystem entry (expected a regular file): {relative}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(src_fd, block_size)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest(), st.st_size, st.st_mode & 0o777
    finally:
        if src_fd >= 0:
            try:
                os.close(src_fd)
            except OSError:
                pass
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass
        os.close(root_fd)


def unlink_regular_nofollow(root: Path, relative: str) -> bool:
    """Unlink a regular file beneath ``root``, refusing links and non-files.

    Returns ``True`` when an entry was removed and ``False`` when the entry was
    already absent (idempotent). A directory, symlink, FIFO, or socket at the
    target raises ``SourcePathError`` rather than being removed.
    """
    parts = relative_parts(relative)
    try:
        root_fd = open_root(root)
    except FileNotFoundError:
        return False
    parent = None
    try:
        try:
            parent = open_parent_nofollow(root_fd, parts[:-1])
        except FileNotFoundError:
            return False
        try:
            st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise SourcePathError(
                f"Cannot delete changed entry: target is not a regular file: {relative}"
            )
        os.unlink(parts[-1], dir_fd=parent)
        return True
    finally:
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass
        os.close(root_fd)


def rmdir_if_empty_nofollow(root: Path, relative: str) -> bool:
    """Remove an *empty* directory beneath ``root`` (received directory -> file).

    Returns ``True`` when removed, ``False`` when absent. A non-empty directory
    raises ``SourcePathError`` so an unrelated live file is never deleted just to
    make a file/directory transition succeed. Symlinks and non-directories are
    never touched (``False`` / error).
    """
    parts = relative_parts(relative)
    try:
        root_fd = open_root(root)
    except FileNotFoundError:
        return False
    parent = None
    try:
        try:
            parent = open_parent_nofollow(root_fd, parts[:-1])
        except FileNotFoundError:
            return False
        try:
            st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode):
            raise SourcePathError(
                f"Cannot replace directory: entry is a symlink: {relative}"
            )
        if not stat.S_ISDIR(st.st_mode):
            return False
        try:
            os.rmdir(parts[-1], dir_fd=parent)
        except OSError as exc:
            raise SourcePathError(
                f"Cannot replace non-empty directory with a file: {relative} ({exc})"
            ) from exc
        return True
    finally:
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass
        os.close(root_fd)


def _copy_fd_bytes(src_fd: int, dst_fd: int, *, block_size: int = 1048576) -> None:
    os.set_blocking(src_fd, True)
    os.set_blocking(dst_fd, True)
    while True:
        chunk = os.read(src_fd, block_size)
        if not chunk:
            break
        os.write(dst_fd, chunk)
