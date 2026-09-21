from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from bunnyland import secure_files


def test_secure_directory_rejects_broken_symlink(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    try:
        broken.symlink_to(tmp_path / "missing")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows process lacks symbolic-link privilege")
        raise
    with pytest.raises(PermissionError, match="regular directory"):
        secure_files.secure_directory(broken / "child")


def test_secure_directory_rejects_file_and_foreign_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.write_text("not a directory", encoding="utf-8")
    with pytest.raises(PermissionError, match="regular directory"):
        secure_files.secure_directory(plain)

    owned = tmp_path / "owned"
    owned.mkdir()
    monkeypatch.setattr(secure_files.sys, "platform", "linux")
    monkeypatch.setattr(secure_files.os, "getuid", lambda: owned.stat().st_uid + 1, raising=False)
    with pytest.raises(PermissionError, match="not owned"):
        secure_files.secure_directory(owned)


def test_secure_directory_rejects_creation_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "created"
    original_lstat = Path.lstat

    def missing_after_create(candidate: Path):
        if candidate == path and candidate.exists():
            raise FileNotFoundError
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", missing_after_create)
    with pytest.raises(PermissionError, match="could not create"):
        secure_files.secure_directory(path)


def test_secure_read_rechecks_descriptor_type_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential"
    path.write_text("secret\n", encoding="utf-8")
    closed: list[int] = []
    original_close = secure_files.os.close
    monkeypatch.setattr(
        secure_files.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=0, st_uid=path.stat().st_uid),
    )

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(secure_files.os, "close", record_close)
    with pytest.raises(PermissionError, match="regular file"):
        secure_files.secure_read_text(path)
    assert len(closed) == 1


def test_secure_write_cleans_temporary_file_when_opened_stream_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential"

    def fail_fdopen(
        _descriptor: int, *_args: object, **_kwargs: object
    ) -> NoReturn:
        raise OSError("stream failed")

    monkeypatch.setattr(secure_files.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="stream failed"):
        secure_files.secure_write_text(path, "secret\n")
    assert not list(tmp_path.glob(".credential.*.tmp"))


def test_windows_read_write_replace_without_getuid(monkeypatch, tmp_path):
    monkeypatch.setattr(secure_files.sys, "platform", "win32")
    monkeypatch.delattr(secure_files.os, "getuid", raising=False)
    path = tmp_path / "private" / "client-id"
    secure_files.secure_write_text(path, "first\n")
    assert secure_files.secure_read_text(path) == "first\n"
    secure_files.secure_write_text(path, "second\n")
    assert secure_files.secure_read_text(path) == "second\n"
    assert not list(path.parent.glob(".*.tmp"))


def test_windows_client_identity_and_claim_round_trip(monkeypatch, tmp_path):
    from bunnyland.tui import backend

    monkeypatch.setattr(secure_files.sys, "platform", "win32")
    monkeypatch.delattr(secure_files.os, "getuid", raising=False)
    monkeypatch.setattr(backend, "CONFIG_DIR", tmp_path)
    path = tmp_path / "client-id"
    client_id = backend.persistent_client_id(path)
    assert backend.persistent_client_id(path) == client_id
    control = backend.ControlClaim("controller:1", 2, "test-claim", "test-secret")
    backend.save_claim_control(client_id, "entity_123", control)
    assert backend.load_claim_control(client_id, "entity_123") == control
