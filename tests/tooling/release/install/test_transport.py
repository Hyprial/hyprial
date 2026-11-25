"""Transport diagnostics and budgets use fake openers, never live credentials."""
from http.client import HTTPException
from pathlib import Path
import ssl
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, proxy_bypass_environment

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SIGNED_URL = "https://sentinel-user:sentinel-password@example.invalid/bucket/wheel.whl?X-Amz-Signature=sentinel-signature"


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import r2_upload
    return r2_upload


@pytest.mark.parametrize("error,diagnostic", [
    (TimeoutError(SIGNED_URL), "TimeoutError"),
    (ConnectionResetError(54, SIGNED_URL), "ConnectionResetError errno 54"),
    (ssl.SSLError(1, SIGNED_URL), "SSLError errno 1"),
    (URLError(TimeoutError(SIGNED_URL)), "URLError reason TimeoutError"),
    (URLError(ConnectionResetError(54, SIGNED_URL)),
     "URLError reason ConnectionResetError errno 54"),
    (URLError(ssl.SSLError(1, SIGNED_URL)), "URLError reason SSLError errno 1"),
    (URLError(SIGNED_URL), "URLError reason str"),
    (HTTPException(SIGNED_URL), "HTTPException"),
])
def test_network_failure_is_diagnostic_and_secret_free(transport, monkeypatch, error, diagnostic):
    attempts, sleeps, checks = [], [], []

    def fail(request, *, timeout):
        attempts.append(timeout)
        raise error

    monkeypatch.setattr(transport, "build_opener", lambda *_: SimpleNamespace(open=fail))
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError) as caught:
        transport.transfer(Request(SIGNED_URL, data=b"wheel", method="PUT"),
                           before_attempt=lambda: checks.append(True))
    message = str(caught.value)
    assert message == f"network error ({diagnostic}): PUT wheel.whl"
    for secret in (SIGNED_URL, "sentinel-user", "sentinel-password", "sentinel-signature",
                   "X-Amz", "example.invalid"):
        assert secret not in message
    assert caught.value.__context__ is None
    assert len(attempts) == len(checks) == 3
    assert sleeps == [10, 60]


@pytest.mark.parametrize("method,size,timeout", [
    ("GET", 0, 15), ("PUT", 0, 15), ("PUT", 128 * 1024, 16),
    ("PUT", 20 * 1024 * 1024, 175),
])
def test_opener_receives_size_budget(transport, monkeypatch, method, size, timeout):
    timeouts = []

    def fail(request, *, timeout):
        timeouts.append(timeout)
        raise TimeoutError("synthetic")

    monkeypatch.setattr(transport, "build_opener", lambda *_: SimpleNamespace(open=fail))
    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    request = Request("https://example.invalid/wheel.whl",
                      data=b"x" * size if method == "PUT" else None, method=method)
    with pytest.raises(RuntimeError):
        transport.transfer(request)
    assert timeouts == [timeout] * 3


@pytest.mark.parametrize("workflow,step_names,runner", [
    ("wheel-release.yml",
     ["Publish wheel track to R2 and verify anonymous read-back"], ["release-ci"]),
    ("release-install-scripts.yml",
     ["Publish installers and verify anonymous read-back",
      "Compare public installer objects with repo bytes"], "ubuntu-latest"),
])
def test_r2_steps_bypass_proxy_without_changing_dependency_routing(workflow, step_names, runner):
    """Proxy bypass stays exact while each workflow keeps its reviewed runner.

    wheel-release publishes from the dedicated release pool (#2137); the
    installer workflow stays on ubuntu-latest. Only the runner pin moved;
    the dependency routing and the NO_PROXY set are unchanged.
    """

    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = {step["name"]: (job, step) for job in doc["jobs"].values()
             for step in job["steps"] if "name" in step}
    for name in step_names:
        job, step = steps[name]
        assert job["runs-on"] == runner
        assert "HTTPS_PROXY" in job["env"]  # Dependency downloads keep their route.
        env = step["env"]
        assert env["NO_PROXY"] == env["no_proxy"]  # urllib prefers lowercase.
        bypass = set(env["no_proxy"].split(","))
        assert {".r2.cloudflarestorage.com", "dl.hyprial.com", "code.hyprial.com"} <= bypass
        assert "*" not in bypass
        proxies = {"no": env["no_proxy"]}
        for host in ("a" * 32 + ".r2.cloudflarestorage.com", "dl.hyprial.com",
                     "code.hyprial.com"):
            assert proxy_bypass_environment(host, proxies)
        assert not proxy_bypass_environment("github.com", proxies)


def test_timeout_math_is_capped_without_allocating_large_bodies(transport):
    assert transport.transfer_timeout("PUT", 1024**3) == 300
    assert transport.transfer_timeout("PUT", 100 * 1024**3) == 300
    assert transport.transfer_timeout("GET", 1024**3) == 15


def test_anonymous_read_back_names_its_user_agent(transport, monkeypatch):
    # dl.hyprial.com's Cloudflare zone answers urllib's default
    # "Python-urllib/x.y" with 403 (error 1010); the read-back must send its own UA.
    seen = []
    monkeypatch.setattr(transport, "transfer", lambda request, **_: seen.append(request))
    obj = SimpleNamespace(key="wheel/internal/hyprial-0.5.5.1-py3-none-any.whl", body=b"wheel")
    transport.verify_public(obj)
    user_agent = seen[0].get_header("User-agent")
    assert user_agent and not user_agent.startswith("Python-urllib")
