"""login: boundaries scenarios."""

from __future__ import annotations
import sys
from pathlib import Path
import pytest
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402
from hyprial.shell.impl.login.flow import (
    USER_AGENT,
    LoginError,
    credential_path,
    refresh_access_token,
)
from hyprial.daemon.impl.configuration.identity import (
    node_owner_or_none,
    resolve_node_owner,
    write_settings_identity,
    write_settings_owner,
)

from .support import (
    _FORBIDDEN,
    _ForwardingProxy,
    _ISSUER_LITERAL,
    _SRC_ROOT,
    _home,
    _profile_for,
    _run,
    _snapshot,
)


def test_t11_daemon_tree_never_imports_login_or_names_the_credential() -> None:
    """The original resident daemon scope follows its finite native owners.

    Relocated old-root join/profile providers and auditable pure public
    bindings are not resident implementations. Unclassified additions fail
    closed; native resident auth imports, readers or calls still go red.
    """

    from tests.integration.native_harnesses.test_native_resident_tripwires import resident_offenders

    offenders = resident_offenders(_SRC_ROOT, _FORBIDDEN)
    assert offenders == {}, (
        "src/hyprial/daemon must not import login or read secrets/login.json "
        f"(offline identity is HYPRIAL_OWNER > settings > error): {offenders}"
    )


def test_r2h_suppression_owner_rejects_login_credential_reader_mutation() -> None:
    """The new resident state owner remains on settings, never login secrets."""

    from tests.integration.native_harnesses.test_native_resident_tripwires import (
        _LOGIN_PATTERN,
        _scan_sources,
    )

    path = "daemon/impl/identity/mutation/suppression.py"
    mutated = "def resident_entry():\n    return read_login_credential()\n"
    assert _scan_sources({path: mutated}, _LOGIN_PATTERN) == {path: [2]}


def test_h1_hook_daemon_files_are_native_and_reject_login_credential_reads() -> None:
    from tests.integration.native_harnesses.test_native_resident_tripwires import (
        NATIVE_ORIGINS,
        _LOGIN_PATTERN,
        _scan_sources,
    )

    for path in (
        "daemon/impl/hooks/__init__.py",
        "daemon/impl/hooks/service.py",
    ):
        assert NATIVE_ORIGINS[path][0] == "resident"
        assert _scan_sources(
            {path: "def injected():\n    return read_login_credential()\n"},
            _LOGIN_PATTERN,
        ) == {path: [2]}


def test_t11_login_docstring_anchor_exists() -> None:
    """The invariant's anchor: the login module docstring must keep the T11
    paragraph — note and tripwire point at each other (U1 T9's pattern)."""

    import hyprial.shell.impl.login.flow as login

    doc = login.__doc__ or ""
    assert "T11 anchor" in doc, (
        "login's docstring must keep the T11 anchor paragraph (daemon must "
        "not import this module nor read secrets/login.json)"
    )
    assert "settings.owner" in doc


def test_t11_daemon_identity_from_settings_without_network_or_credential(
    tmp_path: Path,
) -> None:
    """Offline proof: with no fake OIDC running at all, daemon identity
    resolution reads settings only; the daemon application module imports
    without the login module ever loading (checked in a clean subprocess —
    this test file itself imports login, so the parent's sys.modules would
    lie)."""

    import subprocess

    home = _home(tmp_path)
    write_settings_owner("offline-owner", environ={"HYPRIAL_HOME": str(home)})
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, hyprial.daemon.impl.application\n"
            "assert 'hyprial.login' not in sys.modules, \"daemon import pulled login in\"\n",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=str(_SRC_ROOT.parent),
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "offline-owner"
    assert not credential_path({"HYPRIAL_HOME": str(home)}, hyprial_home=home).exists()


def test_persisted_reserved_owner_loads_at_startup(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text(
        '{"version": 1, "owner": "a.persisted"}\n', encoding="utf-8"
    )
    assert node_owner_or_none({}, hyprial_home=home) == "a.persisted"


@pytest.mark.parametrize(
    "owner",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        # Case-insensitive reservation (APFS): uppercase variants are refused too.
        "U.OU_" + "B" * 32,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_settings_write_boundaries_reject_exact_identity_id_shapes(
    tmp_path: Path, owner: str
) -> None:
    home = _home(tmp_path)
    with pytest.raises(ValueError, match="identity id shape"):
        write_settings_owner(owner, hyprial_home=home)
    with pytest.raises(ValueError, match="identity id shape"):
        write_settings_identity(
            owner,
            mode="casdoor",
            issuer="https://issuer.example",
            hyprial_home=home,
        )


@pytest.mark.parametrize("owner", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_settings_write_boundaries_accept_id_prefix_lookalikes(
    tmp_path: Path, owner: str
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    write_settings_owner(owner, hyprial_home=first)
    write_settings_identity(
        owner,
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=second,
    )
    assert node_owner_or_none({}, hyprial_home=first) == owner
    assert node_owner_or_none({}, hyprial_home=second) == owner


# -- T13: no issuer literal in the new code ---------------------------------------------


def test_t13_no_issuer_literal_outside_default_profile() -> None:
    """U1 T9 already scans all of src/; this pins the new/edited files by
    name so the tripwire visibly covers them (spec T13)."""

    for relative in ("hyprial/shell/impl/login/flow.py", "hyprial/shell/impl/login/oidc.py", "hyprial/shell/impl/login/preview.py", "hyprial/daemon/impl/configuration/network_profile.py", "hyprial/shell/impl/cli/commands/login/commands.py"):
        text = (_SRC_ROOT / relative).read_text(encoding="utf-8")
        if relative == "hyprial/daemon/impl/configuration/network_profile.py":
            assert text.count(_ISSUER_LITERAL) == 1  # the DEFAULT_PROFILE issuer
        else:
            assert _ISSUER_LITERAL not in text, relative


# -- U2b: every outbound request identifies itself ----------------------------------------


def test_u2b_every_request_carries_the_hyprial_user_agent(tmp_path: Path) -> None:
    """The Cloudflare edge in front of the real issuer rejects urllib's default
    ``Python-urllib/3.x`` with HTTP 403 (error 1010).  Discovery, device-auth,
    every token poll, userinfo and refresh must all carry the one module
    constant — removing the header from ``_request_json`` turns this red."""

    assert USER_AGENT.startswith("hyprial-login/")
    assert "urllib" not in USER_AGENT.lower()
    home = _home(tmp_path)
    with fake_oidc() as server:
        _run(server, home)
        refresh_access_token(
            _profile_for(server),
            environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
            hyprial_home=home,
        )
        records = server.records
    assert {record.path for record in records} >= {
        "/.well-known/openid-configuration",
        "/api/device-auth",
        "/api/token",
        "/api/userinfo",
    }
    assert len(records) >= 5
    assert [record.user_agent for record in records] == [USER_AGENT] * len(records)


def test_u2b_discovery_403_names_the_user_agent_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A 403 with an HTML body at discovery is what the edge filter returns;
    the report must say HTTP 403, point at an edge filter and name the
    User-Agent that was sent, instead of a bare DISCOVERY_UNAVAILABLE."""

    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(discovery_status=403)) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "DISCOVERY_UNAVAILABLE"
    message = str(caught.value)
    assert "HTTP 403" in message
    assert "edge filter" in message
    assert USER_AGENT in message
    assert _snapshot(home) == {}


def test_all_proxy_only_shell_routes_the_whole_login_through_the_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    import urllib.request

    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    # urlopen's cached global opener read the environment when first built.
    monkeypatch.setattr(urllib.request, "_opener", None)
    home = _home(tmp_path)
    proxy = _ForwardingProxy()
    try:
        monkeypatch.setenv("ALL_PROXY", proxy.url)
        with fake_oidc(FakeOidcConfig(pending_polls=1)) as server:
            result = _run(server, home)
    finally:
        proxy.close()
    assert result.owner == "alicesmith"
    token_polls = [line for line in proxy.seen if line.startswith("POST ") and line.endswith("/api/token")]
    assert len(token_polls) == 2, proxy.seen  # pending, then success
    assert any("/api/device-auth" in line for line in proxy.seen), proxy.seen
