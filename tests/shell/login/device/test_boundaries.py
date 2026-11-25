"""login: boundaries scenarios."""

from __future__ import annotations
import ast
import json
import subprocess
import sys
import time
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
    _seed_credential,
)


def test_t11_daemon_tree_never_imports_login_or_names_the_credential() -> None:
    """The original resident daemon scope follows its finite native owners.

    Relocated old-root join/profile providers and auditable pure public
    bindings are not resident implementations. Unclassified additions fail
    closed; native resident auth imports, readers or calls still go red.
    """

    from tests.integration.native_harnesses.resident_scan import native_resident_offenders

    offenders = native_resident_offenders(_SRC_ROOT, _FORBIDDEN)
    assert offenders == {}, (
        "src/hyprial/daemon must not import login or read secrets/login.json "
        f"(offline identity is HYPRIAL_OWNER > settings > error): {offenders}"
    )


@pytest.mark.parametrize(
    "path",
    (
        "daemon/impl/onboarding/first_run.py",
        "daemon/impl/request_catalog/onboarding/first_run.py",
        "daemon/impl/application/onboarding/first_run/port.py",
        "daemon/impl/application/onboarding/first_run/selection.py",
        "daemon/impl/application/onboarding/first_run/service.py",
        "daemon/impl/inbox/correlation.py",
        "daemon/impl/onboarding/first_run_attempt.py",
    ),
)
def test_first_run_receipt_owners_reject_login_credential_reads(path: str) -> None:
    from tests.integration.native_harnesses.resident_scan import (
        _LOGIN_PATTERN,
        native_scan_sources,
    )

    assert native_scan_sources({path: "def receipt():\n    return None\n"}, _LOGIN_PATTERN) == {}
    mutated = "def receipt():\n    return read_login_credential()\n"
    assert native_scan_sources({path: mutated}, _LOGIN_PATTERN) == {path: [2]}


def test_r2h_suppression_owner_rejects_login_credential_reader_mutation() -> None:
    """The new resident state owner remains on settings, never login secrets."""

    from tests.integration.native_harnesses.resident_scan import (
        _LOGIN_PATTERN,
        native_scan_sources as _scan_sources,
    )

    path = "daemon/impl/identity/mutation/suppression.py"
    mutated = "def resident_entry():\n    return read_login_credential()\n"
    assert _scan_sources({path: mutated}, _LOGIN_PATTERN) == {path: [2]}


def test_h1_hook_daemon_files_are_native_and_reject_login_credential_reads() -> None:
    from tests.integration.native_harnesses.resident_scan import (
        _LOGIN_PATTERN,
        native_scan_sources as _scan_sources,
    )
    from tests.integration.native_harnesses.test_native_resident_tripwires import NATIVE_ORIGINS

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
            "forbidden = [\n"
            "    'hyprial.login',\n"
            "    'hyprial.identity.impl.onboarding.worker',\n"
            "    'hyprial.identity.impl.onboarding.credentials',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert loaded == [], loaded\n",
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

    for relative in ("hyprial/shell/impl/login/flow.py", "hyprial/shell/impl/login/oidc.py", "hyprial/daemon/impl/configuration/network_profile.py", "hyprial/shell/impl/cli/commands/login/commands.py"):
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
        _seed_credential(home, server.issuer)
        refresh_access_token(
            _profile_for(server),
            environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
            hyprial_home=home,
        )
        records = server.records
    assert {record.path for record in records} >= {
        "/.well-known/openid-configuration",
        "/api/token",
    }
    assert len(records) >= 2
    assert [record.user_agent for record in records] == [USER_AGENT] * len(records)


def test_u2b_discovery_403_names_the_user_agent_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A 403 with an HTML body at discovery is what the edge filter returns;
    the report must say HTTP 403, point at an edge filter and name the
    User-Agent that was sent, instead of a bare DISCOVERY_UNAVAILABLE."""

    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(discovery_status=403)) as server:
        target = _seed_credential(home, server.issuer)
        before = target.read_bytes()
        with pytest.raises(LoginError) as caught:
            refresh_access_token(
                _profile_for(server),
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    assert caught.value.code == "DISCOVERY_UNAVAILABLE"
    message = str(caught.value)
    assert "HTTP 403" in message
    assert "edge filter" in message
    assert USER_AGENT in message
    assert target.read_bytes() == before
    assert not (home / "settings.json").exists()


def test_all_proxy_only_shell_routes_the_whole_login_through_the_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    import urllib.request

    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)
    home = _home(tmp_path)
    proxy = _ForwardingProxy()
    try:
        monkeypatch.setenv("ALL_PROXY", proxy.url)
        with fake_oidc() as server:
            _seed_credential(home, server.issuer)
            access = refresh_access_token(
                _profile_for(server),
                environ={"HYPRIAL_HOME": str(home), "HYPRIAL_OWNER": ""},
                hyprial_home=home,
            )
    finally:
        proxy.close()
    assert access == "fake-access-token-1"
    token_polls = [line for line in proxy.seen if line.startswith("POST ") and line.endswith("/api/token")]
    assert len(token_polls) == 1, proxy.seen
    assert any("/.well-known/openid-configuration" in line for line in proxy.seen), proxy.seen


# -- M2 T11: identity credential worker boundaries --------------------------------------


_IDENTITY_CREDENTIAL_CALLS = frozenset(
    {
        "read_login_credential",
        "read_identity_credential",
        "write_login_credential",
        "write_identity_credential",
        "identity_credential_path",
    }
)


def _identity_credential_offenders(sources: dict[str, str]) -> dict[str, list[int]]:
    offenders: dict[str, list[int]] = {}
    for relative, source in sorted(sources.items()):
        lines: set[int] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hyprial.identity.impl"):
                        lines.add(node.lineno)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("hyprial.identity.impl"):
                    lines.add(node.lineno)
                elif module == "hyprial.identity.impl.onboarding":
                    if any(alias.name in {"credentials", "worker"} for alias in node.names):
                        lines.add(node.lineno)
            elif isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else None
                )
                if name in _IDENTITY_CREDENTIAL_CALLS:
                    lines.add(node.lineno)
        if lines:
            offenders[relative] = sorted(lines)
    return offenders


def test_t11_daemon_never_imports_identity_credential_impl_or_calls_its_readers() -> None:
    daemon_root = _SRC_ROOT / "hyprial" / "daemon"
    sources = {
        path.relative_to(_SRC_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(daemon_root.rglob("*.py"))
    }
    assert _identity_credential_offenders(sources) == {}
    planted = {
        "daemon/impl/onboarding_probe.py": (
            "from hyprial.identity.impl.onboarding.credentials import "
            "read_login_credential\n"
            "def resident(home):\n"
            "    return read_login_credential(home)\n"
        )
    }
    assert _identity_credential_offenders(planted) == {
        "daemon/impl/onboarding_probe.py": [1, 3]
    }


def test_t11_daemon_runtime_reads_settings_not_the_credential_canary(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    secrets = home / "secrets"
    secrets.mkdir(mode=0o700)
    canary = secrets / "login.json"
    canary_text = '{"version": 1, "refreshToken": "canary-refresh-token"}\n'
    canary.write_text(canary_text, encoding="utf-8")
    write_settings_owner("offline-owner", hyprial_home=home)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "home = Path(sys.argv[1])\n"
            "canary = Path(sys.argv[2])\n"
            "canary_real = os.path.realpath(os.fspath(canary))\n"
            "def hook(event, args):\n"
            "    if event not in {'open', 'os.open'} or not args:\n"
            "        return\n"
            "    value = args[0]\n"
            "    if not isinstance(value, (str, bytes, os.PathLike)):\n"
            "        return\n"
            "    if os.path.realpath(os.fsdecode(value)) == canary_real:\n"
            "        raise RuntimeError('daemon read the identity credential')\n"
            "sys.addaudithook(hook)\n"
            "from hyprial.daemon import resolve_node_owner\n"
            "assert resolve_node_owner({'HYPRIAL_OWNER': ''}, hyprial_home=home) == 'offline-owner'\n"
            "import hyprial.daemon.impl.application\n"
            "forbidden = [\n"
            "    'hyprial.identity.impl.onboarding.worker',\n"
            "    'hyprial.identity.impl.onboarding.credentials',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert loaded == [], loaded\n",
            str(home),
            str(canary),
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_SRC_ROOT),
        },
        cwd=str(_SRC_ROOT.parent),
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert canary.read_text(encoding="utf-8") == canary_text


def test_t11_public_identity_proxy_loads_no_worker_credentials_daemon_or_shell() -> None:
    probe = (
        "import sys\n"
        "from hyprial.identity import (\n"
        "    AuthProfile,\n"
        "    CommitPermit,\n"
        "    IdentityCredentialService,\n"
        "    IdentityRecordError,\n"
        "    read_identity_admission,\n"
        "    read_identity_snapshot,\n"
        ")\n"
        "assert IdentityCredentialService and AuthProfile and CommitPermit\n"
        "assert IdentityRecordError and read_identity_admission and read_identity_snapshot\n"
        "forbidden = [\n"
        "    'hyprial.identity.impl.onboarding.worker',\n"
        "    'hyprial.identity.impl.onboarding.credentials',\n"
        "    'hyprial.daemon',\n"
        "    'hyprial.shell',\n"
        "]\n"
        "loaded = [name for name in forbidden if name in sys.modules]\n"
        "assert loaded == [], loaded\n"
    )
    probe_result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_SRC_ROOT),
        },
        cwd=str(_SRC_ROOT.parent),
        timeout=120,
    )
    assert probe_result.returncode == 0, probe_result.stderr


def test_t11_bootstrap_runtime_import_loads_no_worker_credentials_or_shell() -> None:
    probe = (
        "import sys\n"
        "from hyprial.daemon.impl.onboarding.runtime import (\n"
        "    BootstrapError,\n"
        "    BootstrapRuntime,\n"
        ")\n"
        "assert BootstrapRuntime and BootstrapError\n"
        "forbidden = [\n"
        "    'hyprial.identity.impl.onboarding.worker',\n"
        "    'hyprial.identity.impl.onboarding.credentials',\n"
        "    'hyprial.shell',\n"
        "]\n"
        "loaded = [name for name in forbidden if name in sys.modules]\n"
        "assert loaded == [], loaded\n"
    )
    probe_result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_SRC_ROOT),
        },
        cwd=str(_SRC_ROOT.parent),
        timeout=120,
    )
    assert probe_result.returncode == 0, probe_result.stderr


def test_t11_bootstrap_admission_never_opens_the_credential_canary(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    secrets = home / "secrets"
    secrets.mkdir(mode=0o700)
    canary = secrets / "login.json"
    canary_text = '{"version": 1, "refreshToken": "canary-refresh-token"}\n'
    canary.write_text(canary_text, encoding="utf-8")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "home = Path(sys.argv[1])\n"
            "canary = Path(sys.argv[2])\n"
            "canary_real = os.path.realpath(os.fspath(canary))\n"
            "def hook(event, args):\n"
            "    if event not in {'open', 'os.open'} or not args:\n"
            "        return\n"
            "    value = args[0]\n"
            "    if not isinstance(value, (str, bytes, os.PathLike)):\n"
            "        return\n"
            "    if os.path.realpath(os.fsdecode(value)) == canary_real:\n"
            "        raise RuntimeError('daemon read the identity credential')\n"
            "sys.addaudithook(hook)\n"
            "from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime\n"
            "state = home / 'state'\n"
            "runtime = BootstrapRuntime.admit(\n"
            "    home=home, state_dir=state, socket_path=home / 'ipc.sock'\n"
            ")\n"
            "try:\n"
            "    assert runtime.phase == 'awaiting-auth'\n"
            "finally:\n"
            "    runtime.close()\n"
            "assert not (state / 'bootstrap.json').exists()\n"
            "forbidden = [\n"
            "    'hyprial.identity.impl.onboarding.worker',\n"
            "    'hyprial.identity.impl.onboarding.credentials',\n"
            "    'hyprial.shell',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert loaded == [], loaded\n",
            str(home),
            str(canary),
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_SRC_ROOT),
        },
        cwd=str(_SRC_ROOT.parent),
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert canary.read_text(encoding="utf-8") == canary_text


def test_t11_worker_protocol_output_and_receipts_carry_no_credential_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    from dataclasses import asdict
    from hyprial.identity.impl.identity_transaction import IdentityTransactionLock
    from hyprial.identity.impl.onboarding.ports import (
        AuthProfile,
        CommitPermit,
        IdentitySnapshot,
    )
    from hyprial.identity.impl.onboarding.service import IdentityCredentialService

    class RecordingService(IdentityCredentialService):
        def __init__(self) -> None:
            super().__init__()
            self.outputs: list[dict[str, object]] = []

        def begin(self, **kwargs):
            response = super().begin(**kwargs)
            self.outputs.append(asdict(response))
            return response

        def poll_public(self, operation_id: str):
            response = super().poll_public(operation_id)
            self.outputs.append(asdict(response))
            return response

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=1)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        permit = CommitPermit(
            operation_id="op-secret-scan",
            nonce="nonce-secret-scan",
            home=home,
            issuer=profile.issuer,
            client_id=profile.client_id,
            expected_identity=IdentitySnapshot(),
            expires_at=int(time.time()) + 600,
            bootstrap=True,
        )
        lock = IdentityTransactionLock.acquire(home)
        service = RecordingService()
        try:
            begin = service.begin(
                profile=profile, home=home, permit=permit, lock_fd=None
            )
            pending = service.poll_public(begin.operation_id)
            committed = service.poll_public(begin.operation_id)
            stderr = service.stderr_text()
        finally:
            service.close()
            lock.close()
        assert pending.state == "pending"
        assert committed.state == "committed"
        observed = json.dumps(service.outputs)
        observed += json.dumps(
            {
                "pending": pending.receipt,
                "committed": committed.receipt,
                "error": committed.error_message,
            }
        )
        observed += stderr
        for secret in (
            server.config.device_code,
            server.config.access_token,
            server.config.refresh_token,
            server.config.id_token,
        ):
            assert secret not in observed
    credential = json.loads((home / "secrets" / "login.json").read_text(encoding="utf-8"))
    assert credential["refreshToken"] == server.config.refresh_token
    assert "accessToken" not in credential
