"""Tenant-scoped append-only JSONL snapshots with compare-and-swap persistence."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from ..domain import Session


class SessionStorageError(RuntimeError):
    """Persisted data is invalid; do not silently replace it."""


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _loads(value):
    return json.loads(value, object_pairs_hook=_no_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


def _encode(session):
    return {"format": 1, **session.model_dump(mode="json")}


def _decode(item):
    if not isinstance(item, dict) or item.get("format") != 1:
        raise ValueError("unsupported snapshot")
    return Session.model_validate({key: value for key, value in item.items() if key != "format"})


class JsonlSessionStore:
    """One JSONL file per session, isolated by a digest of the trusted scope.

    A missing newline does not invalidate a complete last JSON record. Only an
    invalid, unterminated final line is treated as a crash tail. Complete corrupt
    records fail closed. Locks cover read/CAS/tail repair/append across processes.
    """

    def __init__(self, root, scope: str, *, max_snapshot_bytes=8 * 1024 * 1024):
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("a trusted workspace/project scope is required")
        base = Path(root).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(scope.encode()).hexdigest()
        self.directory = base / digest
        if self.directory.is_symlink():
            raise ValueError("scope directory must not be a symlink")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.max_snapshot_bytes = max_snapshot_bytes

    def _path(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ValueError("invalid session id")
        return self.directory / f"{session_id}.jsonl"

    @contextlib.contextmanager
    def _lock(self, path, exclusive=False):
        fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, 0, False
        session, offset, tail = None, 0, False
        with os.fdopen(fd, "rb") as stream:
            while line := stream.readline(self.max_snapshot_bytes + 2):
                if len(line) > self.max_snapshot_bytes + 1:
                    raise SessionStorageError("session snapshot exceeds size limit")
                terminated = line.endswith(b"\n")
                try:
                    decoded = _decode(_loads(line))
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    if not terminated and stream.read(1) == b"":
                        tail = False
                        break
                    raise SessionStorageError("session contains a corrupt complete record") from exc
                if decoded.id != path.stem or (session and decoded.revision <= session.revision):
                    raise SessionStorageError("session identity or revision history is invalid")
                session = decoded
                offset = stream.tell()
                tail = not terminated
        return session, offset, tail

    def get(self, session_id):
        path = self._path(session_id)
        with self._lock(path):
            return self._read(path)[0]

    def list_sessions(self):
        result = []
        for path in sorted(self.directory.glob("*.jsonl")):
            session = self.get(path.stem)
            if session is not None:
                result.append(session)
        return result

    def save(self, session, expected_revision=None):
        from ..application import SessionConflictError

        path = self._path(session.id)
        payload = json.dumps(_encode(session), ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode()
        if len(payload) > self.max_snapshot_bytes:
            raise ValueError("session snapshot exceeds size limit")
        with self._lock(path, exclusive=True):
            previous, good_offset, needs_separator = self._read(path)
            if previous is None:
                if expected_revision not in (None, 0):
                    raise SessionConflictError("session was not found at the expected revision")
            elif (expected_revision is None or previous.revision != expected_revision
                  or session.revision <= previous.revision):
                raise SessionConflictError("session changed; reload before saving")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+b") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.truncate(good_offset)
                stream.seek(good_offset)
                if previous is not None and needs_separator:
                    stream.write(b"\n")
                stream.write(payload + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            dirfd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
