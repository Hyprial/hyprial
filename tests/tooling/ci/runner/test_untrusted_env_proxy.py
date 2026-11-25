"""Tree code run by CI gates must reach the package index through the job's proxy.

Regression (2026-10-31): pr-hygiene's merge-tree gate ran ``uv run --with pytest``
with an allowlist environment that dropped HTTPS_PROXY/HTTP_PROXY and
UV_HTTP_TIMEOUT, so uv dialed PyPI directly and timed out on proxy-only runners;
every PR's pr-hygiene went red and the test shards were skipped.
"""

from __future__ import annotations

from scripts import git_environment


def test_proxy_and_uv_timeout_and_retries_reach_untrusted_code():
    source = {
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://host.docker.internal:7896",
        "HTTP_PROXY": "http://host.docker.internal:7896",
        "https_proxy": "http://host.docker.internal:7896",
        "ALL_PROXY": "socks5://127.0.0.1:1080",
        "NO_PROXY": "localhost,127.0.0.1",
        "UV_HTTP_TIMEOUT": "60",
        "UV_HTTP_RETRIES": "10",
    }
    env = git_environment.uncredentialed_subprocess_environment(source)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY", "UV_HTTP_TIMEOUT", "UV_HTTP_RETRIES"):
        assert env[name] == source[name]


def test_proxy_with_userinfo_is_not_forwarded():
    source = {
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://user:pass@proxy.example:3128",
        "http_proxy": "http://user@proxy.example:3128",
        "ALL_PROXY": "not a url ::::",
    }
    env = git_environment.uncredentialed_subprocess_environment(source)
    assert "HTTPS_PROXY" not in env
    assert "http_proxy" not in env
    assert "pass" not in " ".join(env.values())


def test_credentials_still_rejected_and_unknown_names_dropped():
    source = {"PATH": "/usr/bin", "GITHUB_TOKEN": "ghs_x", "RANDOM_VAR": "x", "HTTPS_PROXY": "http://p:1"}
    env = git_environment.uncredentialed_subprocess_environment(source)
    assert "GITHUB_TOKEN" not in env
    assert "RANDOM_VAR" not in env
