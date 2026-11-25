"""R1 regressions: real local Git refs plus the loopback S3 publisher."""
import subprocess
from types import SimpleNamespace

import pytest

from test_publish import CREDS
from test_wheels import distribution, wheels as wheels


@pytest.mark.parametrize("track", ["stable", "nightly", "internal"])
@pytest.mark.parametrize("annotated", [False, True])
def test_sequential_stale_retry_cannot_roll_back_manifest(wheels, source_repo, tmp_path, s3, track, annotated):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    distribution(old, f"0.0.1+g{source_repo.old[:7]}")
    distribution(new, f"0.0.2+g{source_repo.new[:7]}")
    source_repo.move(track, source_repo.new, annotated=annotated)
    wheels["publish"](new, track, CREDS, source_sha=source_repo.new, endpoint=s3.url, public_base=s3.url)
    latest = dict(s3.objects)
    s3.events.clear()
    with pytest.raises(RuntimeError, match=f"stale run: {track} now at {source_repo.new}"):
        wheels["publish"](old, track, CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert s3.objects == latest
    assert s3.events == [], "stale source must be rejected before touching the bucket"


@pytest.mark.parametrize("track", ["stable", "nightly", "internal"])
@pytest.mark.parametrize("annotated", [False, True])
def test_mid_upload_move_preserves_manifest(wheels, source_repo, tmp_path, s3, monkeypatch, track, annotated):
    import r2_upload

    names = distribution(tmp_path, f"0.0.1+g{source_repo.old[:7]}")
    prefix = f"wheel/{track}/"
    s3.objects[prefix + "SHA256SUMS"] = b"previous manifest\n"
    original = r2_upload.verify_public

    def move_after_last_wheel(obj, public_base):
        original(obj, public_base)
        if obj.key == prefix + names[-1]:
            source_repo.move(track, source_repo.new, annotated=annotated)

    monkeypatch.setattr(r2_upload, "verify_public", move_after_last_wheel)
    with pytest.raises(RuntimeError, match=f"stale run: {track} now at {source_repo.new}"):
        wheels["publish"](tmp_path, track, CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert [key for method, key in s3.events if method == "PUT"] == [prefix + name for name in names]
    assert s3.objects[prefix + "SHA256SUMS"] == b"previous manifest\n"
    assert s3.events[-1][0] == "LIST", "partial upload still requires the bucket scope guard"


@pytest.mark.parametrize("failure", ["missing", "unreachable"])
def test_unresolvable_track_has_no_uploads(wheels, source_repo, tmp_path, s3, failure):
    distribution(tmp_path)
    if failure == "missing":
        source_repo.git("push", "origin", ":refs/tags/internal")
    else:
        source_repo.git("remote", "set-url", "origin", str(tmp_path / "absent.git"))
    with pytest.raises(RuntimeError):
        wheels["publish"](tmp_path, "internal", CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert s3.events == []


def test_manifest_retry_rechecks_track_after_backoff(wheels, source_repo, tmp_path, s3, monkeypatch):
    import r2_upload

    distribution(tmp_path)
    key = "wheel/internal/SHA256SUMS"
    s3.objects[key] = b"previous manifest\n"
    s3.fail_put = key
    sleeps = []

    def backoff(seconds):
        sleeps.append(seconds)
        assert s3.events.count(("PUT", key)) == 1
        source_repo.move("internal", source_repo.new)

    monkeypatch.setattr(r2_upload, "time", SimpleNamespace(sleep=backoff))
    with pytest.raises(RuntimeError, match=f"stale run: internal now at {source_repo.new}"):
        wheels["publish"](tmp_path, "internal", CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert sleeps == [10]
    assert s3.events.count(("PUT", key)) == 1, "stale retries must stop before another PUT"
    assert s3.objects[key] == b"previous manifest\n"
    assert s3.events[-1][0] == "LIST"


def test_ref_timeout_is_bounded_and_redacted(wheels, monkeypatch, source_repo):
    def timeout(args, **kwargs):
        assert args[:3] == ["git", "ls-remote", "--exit-code"]
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(args, 60, stderr="credential-fixture-do-not-print")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="Cannot resolve source-remote track") as error:
        wheels["require_current_track"]("internal", source_repo.old)
    assert "credential-fixture" not in str(error.value)
