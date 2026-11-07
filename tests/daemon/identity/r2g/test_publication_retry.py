"""R2g retry regressions for organization binding publication."""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile, write_profile
from hyprial.daemon.impl.application.messaging import identity_binding as bridge_module
from hyprial.daemon.impl.identity import bindings as binding_module
from hyprial.daemon.impl.identity.bindings import OrgBindingCacheProjector
from hyprial.daemon.impl.org.network.ipc import _publish_identity_binding_after_join
from hyprial.identity import binding_assertion_publish_after


CLIENT_ID = "1be2763a29154030fbcb"
ALICE = "alice@example.com"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _proof(key: rsa.RSAPrivateKey, *, expires_at: float) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": "key-a"}).encode())
    payload = _b64(
        json.dumps(
            {
                "iss": "https://issuer.invalid",
                "aud": CLIENT_ID,
                "name": ALICE,
                "owner": "hyprial",
                "oauth_Lark_unionId": "on_alice",
                "iat": 100,
                "exp": expires_at,
            }
        ).encode()
    )
    signed = f"{header}.{payload}".encode()
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _write_binding(home: Path, proof: str) -> None:
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "identityBinding": {
                    "user": ALICE,
                    "larkUnionId": "on_alice",
                    "proof": proof,
                    "publishedAt": "2026-10-07T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )


class _Cache:
    next_recheck_at = None

    def rebuild(self) -> bool:
        return True


class _RetryDirectory:
    def __init__(self) -> None:
        self.attempts = 0
        self.published = threading.Event()

    def binding_watch_targets(self) -> list[tuple[str, str]]:
        return []

    def orgs(self) -> list[str]:
        return ["group-example"]

    def put_binding(self, _org: str, _row: dict[str, object]) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient publication failure")
        self.published.set()


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda app: app._start_org_identity_bindings_safely(), id="startup"
        ),
        pytest.param(_publish_identity_binding_after_join, id="join"),
    ],
)
def test_immediate_publish_failure_is_retried_by_projector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[Any], None],
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    home = tmp_path / "home"
    _write_binding(home, _proof(key, expires_at=10))
    directory = _RetryDirectory()
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=directory, facade=object()
    )
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = projector
    app._start_org_identity_binding_cache = lambda: None  # type: ignore[method-assign]
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.02
    )

    try:
        projector.start()
        invoke(app)
        assert directory.published.wait(1.0)
    finally:
        projector.close()

    assert directory.attempts == 2


def test_deferred_publish_without_projector_starts_one_before_scheduling(
    tmp_path: Path,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    home = tmp_path / "home"
    _write_binding(home, _proof(key, expires_at=time.time() + 3_600))
    scheduled: list[tuple[float, Callable[[], None]]] = []

    class Projector:
        def schedule_publish(
            self, publish_at: float, publisher: Callable[[], None]
        ) -> None:
            scheduled.append((publish_at, publisher))

    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = None
    app._start_org_identity_binding_cache = (  # type: ignore[method-assign]
        lambda: setattr(app, "_org_identity_binding_projector", Projector())
    )
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    app._publish_identity_binding()

    assert len(scheduled) == 1
    assert scheduled[0][0] > time.time()


def test_publication_retry_uses_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[float] = []
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=_RetryDirectory(), facade=object()
    )
    monkeypatch.setattr(binding_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 2.0
    )
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_MAX_SECONDS", 8.0
    )
    projector.schedule_publish = (  # type: ignore[method-assign]
        lambda publish_at, _publisher, **_kwargs: scheduled.append(publish_at)
    )

    for attempt in (0, 1, 2, 20):
        projector.retry_publish(lambda: None, attempt=attempt)

    assert scheduled == [102.0, 104.0, 108.0, 108.0]


def test_retry_targets_only_orgs_that_failed_the_previous_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    home = tmp_path / "home"
    _write_binding(home, _proof(key, expires_at=10))
    calls: list[str] = []
    retried = threading.Event()

    class Directory:
        def binding_watch_targets(self) -> list[tuple[str, str]]:
            return []

        def orgs(self) -> list[str]:
            return ["group-failing", "group-good"]

        def put_binding(self, org: str, _row: dict[str, object]) -> None:
            calls.append(org)
            if org == "group-failing" and calls.count(org) == 1:
                raise RuntimeError("transient publication failure")
            if org == "group-failing":
                retried.set()

    directory = Directory()
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=directory, facade=object()
    )
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = projector
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.02
    )

    try:
        projector.start()
        with pytest.raises(RuntimeError):
            app._publish_identity_binding()
        assert retried.wait(1.0)
    finally:
        projector.close()

    assert calls == ["group-failing", "group-good", "group-failing"]


def test_permanent_publish_failure_warning_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    attempted_three_times = threading.Event()
    logs: list[str] = []

    def publish() -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            attempted_three_times.set()
        raise RuntimeError("permanent publication failure")

    projector = OrgBindingCacheProjector(
        cache=_Cache(),
        directory=_RetryDirectory(),
        facade=object(),
        logger=lambda _level, event, **_fields: logs.append(event),
    )
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.01
    )
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_MAX_SECONDS", 0.02
    )
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS", 1.0
    )

    try:
        projector.start()
        projector.schedule_publish(time.time(), publish)
        assert attempted_three_times.wait(1.0)
    finally:
        projector.close()

    failures = [
        event
        for event in logs
        if event == "identity.org-binding.deferred-publish-failed"
    ]
    assert failures == ["identity.org-binding.deferred-publish-failed"]


def test_per_org_publish_failure_warning_is_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    home = tmp_path / "home"
    _write_binding(home, _proof(key, expires_at=10))
    logs: list[tuple[str, str | None]] = []

    class Directory:
        def orgs(self) -> list[str]:
            return ["group-failing"]

        def put_binding(self, _org: str, _row: dict[str, object]) -> None:
            raise RuntimeError("permanent publication failure")

    class Projector:
        def refresh(self) -> None:
            pass

    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = Projector()
    app._org_binding_directory = lambda: Directory()  # type: ignore[method-assign]
    app._log = (  # type: ignore[method-assign]
        lambda _level, _scope, event, **fields: logs.append(
            (event, fields.get("org"))
        )
    )
    observed = iter((100.0, 101.0, 111.0))
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: next(observed))
    monkeypatch.setattr(
        bridge_module,
        "IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS",
        10.0,
        raising=False,
    )

    for _ in range(3):
        with pytest.raises(RuntimeError):
            app._publish_identity_binding(projector_owned=True)

    failures = [entry for entry in logs if entry[0].endswith("publish-failed")]
    assert failures == [
        ("identity.org-binding.publish-failed", "group-failing"),
        ("identity.org-binding.publish-failed", "group-failing"),
    ]


@pytest.mark.parametrize("expires_at", [1.7e308, 10**400])
def test_unverified_publish_parser_rejects_oversized_exp(
    expires_at: float | int,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(ValueError, match="expiry is missing or invalid"):
        binding_assertion_publish_after(_proof(key, expires_at=expires_at))


def test_binding_cache_verifies_proofs_against_the_resolved_profile_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding proof issuer is the network's, never a literal.

    0.5.3 blocker: a hard-coded ``https://auth.hyprial.com`` would verify a
    self-hosted network's proofs against the wrong issuer.  The client id is
    a separate Casdoor app and stays the identity domain's constant.
    """

    home = tmp_path / "home"
    write_profile(
        NetworkProfile(
            org="acme", issuer="https://id.acme.example", client_id="acme-client"
        ),
        hyprial_home=home,
    )
    seen: dict[str, object] = {}

    class Source:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    class Projector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(binding_module, "OrgBindingSource", Source)
    monkeypatch.setattr(binding_module, "OrgBindingCache", lambda *a, **k: object())
    monkeypatch.setattr(binding_module, "OrgBindingCacheProjector", Projector)
    app = object.__new__(DaemonApplication)
    app.hyprial_home = home
    app.state_dir = tmp_path / "state"
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = None
    app._org_binding_directory = lambda: object()  # type: ignore[method-assign]
    app._require_orgfs_runtime = lambda: type("R", (), {"facade": object()})()  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    app._start_org_identity_binding_cache()

    assert seen["issuer"] == "https://id.acme.example"
    assert seen["client_id"] == CLIENT_ID

