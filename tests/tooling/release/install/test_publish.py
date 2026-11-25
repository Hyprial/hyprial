"""No cloud credentials or uploads: exercise publication on loopback only."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import runpy

import pytest
import yaml

from tests.support.ci import ci_workflow_policy as policy

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/publish_installers.py"
CREDS = {"R2_ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
         "R2_ACCESS_KEY_ID": "test-access", "R2_SECRET_ACCESS_KEY": "test-secret",
         "R2_BUCKET": "test-bucket"}
NAMES = ["install.sh", "install.sh.sha256", "install.ps1", "install.ps1.sha256"]


@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    module = runpy.run_path(str(SCRIPT))
    import time
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    return module


def sources(tmp_path):
    for name in ("install.sh", "install.ps1"):
        (tmp_path / name).write_bytes(f"test {name}\n".encode())
    return tmp_path


def test_signed_put_shape(publisher):
    request = publisher["signed_put"](
        "https://example.r2.cloudflarestorage.com/bucket/install.sh", b"hello",
        "text/x-shellscript", "test-access", "test-secret",
        datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.method == "PUT" and request.data == b"hello"
    assert headers["x-amz-content-sha256"] == sha256(b"hello").hexdigest()
    assert headers["x-amz-date"] == "20261026T000000Z"
    assert headers["cache-control"] == "max-age=60, must-revalidate"
    assert headers["content-type"] == "text/x-shellscript"
    assert "Credential=test-access/20261026/auto/s3/aws4_request" in headers["authorization"]
    assert "SignedHeaders=cache-control;content-type;host;x-amz-content-sha256;x-amz-date" in headers["authorization"]
    assert "test-secret" not in str(headers)


@pytest.mark.parametrize("missing", CREDS)
def test_credentials_fail_explicitly_before_network(publisher, tmp_path, missing):
    env = {key: value for key, value in CREDS.items() if key != missing}
    with pytest.raises(RuntimeError, match="R2 credentials not configured"):
        publisher["publish"](tmp_path, env)


@pytest.mark.parametrize("mismatch", [False, True])
def test_upload_order_and_anonymous_readback(publisher, tmp_path, s3, mismatch):
    if mismatch:
        s3.bad_read = "install.sh"
        with pytest.raises(RuntimeError, match="Anonymous read-back mismatch"):
            publisher["publish"](sources(tmp_path), CREDS, endpoint=s3.url, public_base=s3.url)
    else:
        publisher["publish"](sources(tmp_path), CREDS, endpoint=s3.url, public_base=s3.url)
        assert [key for method, key in s3.events if method == "PUT"] == NAMES
        assert [key for method, key in s3.events if method == "GET"] == NAMES
        for name in ("install.sh", "install.ps1"):
            expected = f"{sha256(s3.objects[name]).hexdigest()}  {name}\n".encode()
            assert s3.objects[f"{name}.sha256"] == expected
    assert s3.events[0][0] == "LIST"
    assert s3.events[-1][0] == "LIST", "guard must run even after read-back failure"
    assert any("continuation-token" in query for method, query in s3.events if method == "LIST")


@pytest.mark.parametrize("change", ["added", "removed", "changed"])
def test_unrelated_bucket_changes_fail(publisher, tmp_path, s3, change):
    s3.outside_change = change
    with pytest.raises(RuntimeError, match="outside allowed scope"):
        publisher["publish"](sources(tmp_path), CREDS, endpoint=s3.url, public_base=s3.url)


@pytest.mark.parametrize("broken", ["list_denied", "wrong_list"])
def test_bucket_guard_fails_closed_before_put(publisher, tmp_path, s3, broken):
    setattr(s3, broken, True)
    with pytest.raises(RuntimeError):
        publisher["publish"](sources(tmp_path), CREDS, endpoint=s3.url, public_base=s3.url)
    assert all(method == "LIST" for method, _key in s3.events)


def test_installer_allowlist(publisher, tmp_path):
    objects = publisher["installer_objects"](sources(tmp_path))
    assert [obj.key for obj in objects] == NAMES
    assert publisher["INSTALLER_KEYS"] == frozenset(NAMES)


@pytest.mark.parametrize("drift", [False, True])
def test_drift_check_needs_no_credentials_or_s3(publisher, tmp_path, s3, drift, monkeypatch):
    sources(tmp_path)
    for name in ("install.sh", "install.ps1"):
        body = (tmp_path / name).read_bytes()
        s3.objects[name] = body
        s3.objects[name + ".sha256"] = f"{sha256(body).hexdigest()}  {name}\n".encode()
    for name in CREDS:
        monkeypatch.delenv(name, raising=False)
    if drift:
        s3.bad_read = "install.ps1.sha256"
        with pytest.raises(RuntimeError, match="dl out of sync with repo"):
            publisher["check_drift"](tmp_path, public_base=s3.url)
    else:
        publisher["check_drift"](tmp_path, public_base=s3.url)
    assert all(method == "GET" for method, _key in s3.events)


def test_workflow_is_dev_only_and_policy_compliant():
    path = ROOT / ".forgejo/workflows/release-install-scripts.yml"
    text = path.read_text()
    doc = yaml.safe_load(text)
    triggers = doc.get("on", doc.get(True))
    assert triggers["push"]["branches"] == ["dev"]
    assert {"install.sh", "install.ps1"} <= set(triggers["push"]["paths"])
    assert "workflow_dispatch" in triggers
    job = doc["jobs"]["publish"]
    assert "refs/heads/dev" in job["if"] and "vars." not in job["if"]
    assert job["timeout-minutes"] <= 15
    assert doc["concurrency"]["cancel-in-progress"] is False
    refs = policy.runner_jobs_in(text, path)
    assert policy.missing_tail_jobs(refs)[1] == []
    assert policy.relaxed_gate_steps(refs) == []
    assert policy.unpinned_uses_offenders_in(text) == []
    publish = next(step for step in job["steps"] if "publish_installers.py" in step.get("run", ""))
    assert set(CREDS) <= set(publish["env"])
    assert "uv run --no-project --python 3.12 python" in publish["run"]
    assert not set(CREDS).intersection(job.get("env", {}))
    drift = doc["jobs"]["drift"]
    assert drift["needs"] == ["publish"]
    assert "always()" in drift["if"]
    assert "secrets.R2" not in str(drift)
    assert "--check" in str(drift)
