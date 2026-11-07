"""Hardened environments for credentialed Git and untrusted tree code.

Credentialed Git subprocesses must not inherit repository selectors, transport
hooks, or configuration paths from the job environment.  Subprocesses that run
code from a checked-out tree receive only explicitly safe process settings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

GitIdentity = tuple[str, str]
GitConfigEntry = tuple[str, str]

_UNTRUSTED_CODE_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CI",
        "COLORTERM",
        "CURL_CA_BUNDLE",
        "FORCE_COLOR",
        "HOME",
        "LANG",
        "LANGUAGE",
        "NO_COLOR",
        "NO_PROXY",
        "PATH",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "UV_CACHE_DIR",
        "UV_HTTP_TIMEOUT",
        "UV_LINK_MODE",
        "UV_OFFLINE",
        "UV_PYTHON_INSTALL_DIR",
        "no_proxy",
    }
)
_CREDENTIAL_ENVIRONMENT_MARKERS = ("TOKEN", "SECRET", "PASSWORD")
# Egress proxy settings: tree code (e.g. `uv run --with pytest`) must reach the
# package index the same way the job does, or it dials out directly and times
# out on proxy-only runners. A proxy URL that carries userinfo is a credential
# and is never forwarded to untrusted code.
_PROXY_ENVIRONMENT_NAMES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
)


def _proxy_without_credentials(value: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value if "://" in value else f"http://{value}")
    except ValueError:
        return False
    return parts.username is None and parts.password is None and bool(parts.hostname)


def credential_environment_names(source: Mapping[str, str]) -> tuple[str, ...]:
    """Return variable names that can designate subprocess credentials."""

    found = []
    for name in source:
        upper = name.upper()
        if (
            any(marker in upper for marker in _CREDENTIAL_ENVIRONMENT_MARKERS)
            or upper == "GIT_ASKPASS"
            or upper.startswith("GIT_CONFIG_")
        ):
            found.append(name)
    return tuple(sorted(found))


def assert_no_credentials(source: Mapping[str, str]) -> None:
    """Reject a child environment containing a credential-shaped variable."""

    names = credential_environment_names(source)
    if names:
        raise AssertionError(
            "credential variables in untrusted subprocess environment: "
            + ", ".join(names)
        )


def uncredentialed_subprocess_environment(
    source: Mapping[str, str],
) -> dict[str, str]:
    """Build the strict allowlist environment for code from an untrusted tree."""

    env = {
        key: value
        for key, value in source.items()
        if key in _UNTRUSTED_CODE_ENVIRONMENT_ALLOWLIST or key.startswith("LC_")
    }
    env.update(
        {
            key: value
            for key, value in source.items()
            if key in _PROXY_ENVIRONMENT_NAMES and _proxy_without_credentials(value)
        }
    )
    assert_no_credentials(env)
    return env


def ignored_git_environment_names(source: Mapping[str, str]) -> tuple[str, ...]:
    """Return inherited Git/XDG controls that hardening will discard."""

    return tuple(
        sorted(
            key
            for key in source
            if key.startswith("GIT_") or key == "XDG_CONFIG_HOME"
        )
    )


def ignored_git_environment_warning(source: Mapping[str, str]) -> str | None:
    """Describe discarded variable names without exposing their values."""

    names = ignored_git_environment_names(source)
    if not names:
        return None
    safe_names = (
        name
        if name.isascii() and name.replace("_", "").isalnum()
        else repr(name)
        for name in names
    )
    return "warning: ignored inherited git environment: " + ", ".join(safe_names)


class ProxyCredentialError(RuntimeError):
    """代理变量的取值内含 userinfo（`scheme://user:pass@host`），拒绝传播。

    刻意用 `raise` 而不是 `assert`: `python -O` 会把 assert 整条去掉，
    那样这条拒绝就会在"优化运行"里悄悄消失（而 CI 里恰好有 `-O` 的场景不可控）。
    """


#: 只有这些代理变量允许进入加固后的 git 环境。固定名单,不做前缀匹配。
_PROXY_ENVIRONMENT_KEYS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)


def _proxy_environment(source: Mapping[str, str]) -> dict[str, str]:
    """取代理变量，但**拒绝**任何带用户信息的取值。

    ⛔ 2026-10-30 现场（run 40868 / task 98850）: `github_publish` 的 git 命令走
    `hardened_git_environment` 的窄名单（只留 PATH/LANG/TMPDIR/LC_*），
    于是 job env 里明明有 `HTTPS_PROXY=http://host.docker.internal:7896`（run 自己的 setupEnv 有记录），
    clone 却是**裸连 github.com** ⇒ `GnuTLS recv error (-110)` / `SSL connection timeout` /
    `expected flush after ref listing`；而同一台机器经该代理 clone 只要 4-5 s。
    代理不是凭据，但它可能**内含凭据**（`http://user:pass@host:port`），所以：
      · 名单固定（不做前缀匹配，挡住 HTTPS_PROXY_FOO 之类）
      · 取值里出现 `@`（userinfo）就拒绝，而不是把它悄悄写进子进程环境
    """
    picked: dict[str, str] = {}
    for key in _PROXY_ENVIRONMENT_KEYS:
        value = source.get(key)
        if value is None or not value:
            continue
        if "@" in value:
            raise ProxyCredentialError(
                f"proxy variable {key} embeds credentials; refusing to propagate it"
            )
        picked[key] = value
    return picked


def hardened_git_environment(
    source: Mapping[str, str],
    *,
    config_entry: GitConfigEntry | None = None,
    askpass: str | Path | None = None,
    credential_variables: Mapping[str, str] | None = None,
    identity: GitIdentity | None = None,
    include_home: bool = False,
    propagate_proxy: bool = False,
) -> dict[str, str]:
    """Build a credentialed Git environment from a narrow allowlist.

    No inherited ``GIT_*`` or XDG variable is copied.  A command-scope config
    credential always occupies entry zero rather than extending attacker-
    supplied entries.  ``credential_variables`` is for values an explicitly
    injected askpass program needs; it is never populated from ``source``
    implicitly.
    """

    env = {
        key: value
        for key, value in source.items()
        if key in {"PATH", "LANG", "TMPDIR"} or key.startswith("LC_")
    }
    if include_home and "HOME" in source:
        env["HOME"] = source["HOME"]
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if propagate_proxy:
        env.update(_proxy_environment(source))
    if config_entry is not None:
        key, value = config_entry
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": key,
                "GIT_CONFIG_VALUE_0": value,
            }
        )
    if askpass is not None:
        env["GIT_ASKPASS"] = os.fspath(askpass)
    if credential_variables:
        env.update(credential_variables)
    if identity is not None:
        name, email = identity
        env.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
            }
        )
    return env
