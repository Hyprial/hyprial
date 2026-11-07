"""The publication lock the daemon constructor creates must be re-entrant.

0.5.3 regression (reported from a member's rejoin, 2026-10-08): a daemon that
starts before the binding proof's ``publish_after`` takes the deferred branch.
``_publish_identity_binding`` holds the publication lock and the defer path
takes it again (``_add_pending_identity_binding_publication``).  The lazily
created lock is an ``RLock``, but ``DaemonApplication.__init__`` created a
plain ``Lock``, so startup hung for good at the transport phase.  Every other
test built the application with ``object.__new__`` and so never saw the
constructor's lock; these build it with the real constructor.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

_HANG_TIMEOUT_SECONDS = 10.0


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _proof(key: rsa.RSAPrivateKey, *, expires_at: float) -> str:
    """A binding assertion whose publish_after lies in the future."""

    header = _b64(json.dumps({"alg": "RS256", "kid": "key-a"}).encode())
    payload = _b64(
        json.dumps(
            {
                "iss": "https://issuer.invalid",
                "aud": "1be2763a29154030fbcb",
                "name": "alice@example.com",
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
                    "user": "alice@example.com",
                    "larkUnionId": "on_alice",
                    "proof": proof,
                    "publishedAt": "2026-10-07T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )


def _constructed_app(tmp_path: Path) -> DaemonApplication:
    state_dir = tmp_path / "state"
    (state_dir / "logs" / "workers").mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {"schemaVersion": 1, "asMailbox": False, "providers": [], "interactiveSessions": []}
        ),
        encoding="utf-8",
    )
    return DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="alice@example.com",
        **daemon_dependencies(),
    )


def _finishes(action: Callable[[], None]) -> tuple[bool, BaseException | None]:
    failure: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            failure.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_HANG_TIMEOUT_SECONDS)
    return not worker.is_alive(), (failure[0] if failure else None)


def _deferring(app: DaemonApplication, tmp_path: Path) -> list[tuple[float, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    home = tmp_path / "home"
    _write_binding(home, _proof(key, expires_at=time.time() + 3_600))
    scheduled: list[tuple[float, Any]] = []

    class Projector:
        def schedule_publish(self, publish_at: float, publisher: Any) -> None:
            scheduled.append((publish_at, publisher))

        def close(self) -> None:
            return None

    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app._org_identity_binding_projector = Projector()
    return scheduled


def test_a_daemon_started_before_publish_after_defers_instead_of_hanging(tmp_path: Path) -> None:
    app = _constructed_app(tmp_path)
    try:
        scheduled = _deferring(app, tmp_path)
        app._start_org_identity_binding_cache = lambda: None  # type: ignore[method-assign]
        # The daemon's own start entry (wiring/transport.py), as in the report.
        done, failure = _finishes(app._start_org_identity_bindings_safely)
    finally:
        app._close()

    assert done, "publication deadlocked on the constructor's publication lock"
    assert failure is None
    assert len(scheduled) == 1 and scheduled[0][0] > time.time()


def test_the_explicit_publish_command_also_defers_under_the_lock(tmp_path: Path) -> None:
    """``identity binding publish`` holds the lock around the same body."""

    app = _constructed_app(tmp_path)

    def publish_while_holding() -> None:
        with app._identity_binding_publication_lock():
            app._publish_identity_binding(orgs=("group-acme",))

    try:
        scheduled = _deferring(app, tmp_path)
        done, failure = _finishes(publish_while_holding)
    finally:
        app._close()

    assert done, "a nested publication deadlocked on the constructor's lock"
    assert failure is None
    assert [publish_at > time.time() for publish_at, _ in scheduled] == [True]
