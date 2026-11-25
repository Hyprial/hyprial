"""Portable digest observation uses lstat and reparse checks without dir_fd."""

import os
from pathlib import Path
from types import SimpleNamespace

from hyprial.identity.impl.agents.home.native_imports import manifest


def test_observation_succeeds_without_posix_dir_fd_or_nofollow_flags(monkeypatch, tmp_path):
    payload = b"isolated-test-auth"
    target = tmp_path / "auth.json"
    target.write_bytes(payload)
    target.chmod(0o666)
    platform = SimpleNamespace(fstat=os.fstat, supports_dir_fd=frozenset())
    monkeypatch.setattr(manifest, "os", platform)

    assert manifest.observe(tmp_path, {"auth.json"}) == manifest.document_digest([
        ("auth.json", payload),
    ])


def test_observation_refuses_symlinks_reparse_points_and_staging_residue(monkeypatch, tmp_path):
    payload = b"isolated-test-auth"
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload)
    root = tmp_path / "native"
    root.mkdir()
    target = root / "auth.json"
    target.symlink_to(outside)
    assert manifest.observe(root, {"auth.json"}) is None

    target.unlink()
    target.write_bytes(payload)
    assert manifest.observe(root, {"auth.json"}) == manifest.document_digest([("auth.json", payload)])

    (root / "auth.json.incoming").write_bytes(b"staging")
    assert manifest.observe(root, {"auth.json"}) is None
    (root / "auth.json.incoming").unlink()

    real_lstat = Path.lstat

    def reparse_lstat(self: Path):
        metadata = real_lstat(self)
        if self == target:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_size=metadata.st_size,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_mtime_ns=metadata.st_mtime_ns,
                st_file_attributes=0x400,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    assert manifest.observe(root, {"auth.json"}) is None
