import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from hyprial.identity import TailnetCredentialError, TransportCredentialError
from . import Context, inventory, stage, targets


def denied_without_mutation(w, h, home):
    before = inventory(home)
    error = None
    try:
        w.discard_staged(h, confirmed=True)
    except (TailnetCredentialError, TransportCredentialError, OSError) as caught:
        error = caught
    assert inventory(home) == before, "preflight allowed actual staging deletion"
    assert isinstance(error, (TailnetCredentialError, TransportCredentialError))
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "field",
    ["operationId", "owner", "deviceUid", "expectedCredentialGeneration", "spkiSha256"],
)
def test_enrollment_five_fields_match_frozen_operation_before_delete(tmp_path, field):
    w = Context(tmp_path).workflow()
    h = stage(w)
    _, enrollment = targets(tmp_path, w, h)
    payload = json.loads(enrollment.read_bytes())
    payload[field] = {
        "operationId": str(uuid4()),
        "owner": "other",
        "deviceUid": uuid4().hex,
        "expectedCredentialGeneration": 1,
        "spkiSha256": "0" * 64,
    }[field]
    enrollment.write_text(json.dumps(payload))
    denied_without_mutation(w, h, tmp_path)


@pytest.mark.parametrize(
    "case",
    ["key-spki", "home", "path", "installed-spki", "missing-key", "missing-enrollment"],
)
def test_frozen_paths_key_binding_and_installed_key_are_checked(tmp_path, case):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    op = w._operations[h]
    if case == "key-spki":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key.write_bytes(
            other.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    elif case == "home":
        op.staged = replace(op.staged, _home=tmp_path / "other")
    elif case == "path":
        other = key.with_name("other.pem")
        other.write_bytes(key.read_bytes())
        other.chmod(0o600)
        op.staged = replace(op.staged, _key_path=other)
    elif case == "installed-spki":
        committed = tmp_path / "secrets/transport/device-key.pem"
        committed.write_bytes(key.read_bytes())
        committed.chmod(0o600)
    elif case == "missing-key":
        key.unlink()
    else:
        enrollment.unlink()
    denied_without_mutation(w, h, tmp_path)


@pytest.mark.parametrize("target", ["key", "enrollment", "committed"])
@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "directory", "mode", "uid"]
)
def test_all_file_safety_checks_precede_first_unlink(
    tmp_path, monkeypatch, target, kind
):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    path = {
        "key": key,
        "enrollment": enrollment,
        "committed": tmp_path / "secrets/transport/device-key.pem",
    }[target]
    if target == "committed":
        path.write_bytes(key.read_bytes())
        path.chmod(0o600)
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "uid":
        original = os.lstat

        def wrong_uid(candidate, *args, **kwargs):
            kwargs.pop("follow_symlinks", None)
            result = original(candidate, *args, **kwargs)
            if candidate == path:
                values = list(result)
                values[4] += 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "lstat", wrong_uid)
        monkeypatch.setattr(os, "stat", wrong_uid)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(tmp_path / "outside-missing")
        if kind == "hardlink":
            outside = tmp_path / "other-file"
            outside.write_text("unrelated")
            outside.chmod(0o600)
            os.link(outside, path)
        if kind == "fifo":
            os.mkfifo(path, 0o600)
        if kind == "directory":
            path.mkdir(mode=0o600)
    denied_without_mutation(w, h, tmp_path)


@pytest.mark.parametrize(
    "directory",
    [
        "home",
        "secrets",
        "secrets/transport",
        "secrets/transport/staged",
        "operation",
        "state",
        "state/transport",
    ],
)
def test_directory_owner_is_checked_without_deleting(tmp_path, monkeypatch, directory):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, _ = targets(tmp_path, w, h)
    path = (
        tmp_path
        if directory == "home"
        else key.parent
        if directory == "operation"
        else tmp_path / directory
    )
    original_stat, original_lstat = os.stat, os.lstat
    inode = original_lstat(path).st_ino

    def fake_owner(result):
        if result.st_ino == inode:
            values = list(result)
            values[4] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", lambda *a, **k: fake_owner(original_stat(*a, **k)))
    monkeypatch.setattr(
        os, "lstat", lambda *a, **k: fake_owner(original_lstat(*a, **k))
    )
    denied_without_mutation(w, h, tmp_path)


@pytest.mark.parametrize("target", ["operation", "state"])
@pytest.mark.parametrize("kind", ["symlink", "mode"])
def test_directory_type_and_mode_are_checked(tmp_path, target, kind):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    path = key.parent if target == "operation" else enrollment.parent
    if kind == "mode":
        path.chmod(0o755)
    else:
        moved = path.with_name(path.name + "-moved")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    denied_without_mutation(w, h, tmp_path)
