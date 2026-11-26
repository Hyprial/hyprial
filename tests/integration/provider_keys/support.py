"""Cross-slice seams for the owner-key end-to-end tests.

The single-replica OrgFS runtime, enabled test U1 signer, verified anchor and real
X1 store come from the service-slice ``World`` (tests/daemon/provider_keys/service).
This module adds what joins the slices: the real root CLI whose ``_daemon_request``
calls ``METHOD_REGISTRY.invoke`` with origin "ipc", the daemon's managed-worker
launch composition, and a real ``DaemonApplication`` for ``_handle_agent``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.daemon.impl.application import DaemonApplication, compose_daemon_worker_launch
from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.daemon.provider_keys.ipc.conftest import make_app
from tests.daemon.provider_keys.service.conftest import OWNER, Anchor, World, bound_signer
from tests.support.runtime.environment import UNIT_USER_ID

ENV = "DEEPSEEK_API_KEY"
CLI_SECRET = "sk-E2E-CLI-SENTINEL-31415926-plaintext"
GUI_SECRET = "sk-E2E-GUI-SENTINEL-27182818-plaintext"
LEGACY_SECRET = "sk-E2E-LEGACY-SENTINEL-16180339"
INJECTED = ("_user_key_signer", "_provider_key_space_resolver", "_provider_key_owner_key_port",
            "_provider_key_clock")


class _Unused:
    pass


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


def ipc_app(world: World, *, fresh_signer: bool = False) -> Any:
    """A daemon-shaped app on the world's home, state and registry, with no injected service.

    ``fresh_signer`` builds new signer, anchor and resolver objects over the same
    signer state and OrgFS space, as a restarted daemon would.
    """
    instance = make_app(world.tmp)
    for name in ("hyprial_home", "state_dir", "owner", "agents", *INJECTED):
        setattr(instance, name, getattr(world.app, name))
    instance.desired_state = DesiredStateStore(world.state / "desired-state.json")
    if fresh_signer:
        wire(instance, world, bound_signer(world.state))
    return instance


def invoke(app: Any, method: str, params: dict) -> Any:
    return METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin="ipc")


def refused(app: Any, method: str, params: dict) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app, method, params)
    return caught.value


def cli(app: Any, monkeypatch: pytest.MonkeyPatch, arguments: list[str], *, stdin: str | None = None) -> Any:
    """Run the real root CLI; its daemon request is a registry invoke with origin "ipc"."""
    sent: list[tuple[str, dict]] = []

    def request(method: str, params: dict | None = None, **_kwargs: Any) -> Any:
        sent.append((method, dict(params or {})))
        try:
            return invoke(app, method, dict(params or {}))
        except DaemonRequestError as error:
            raise get_services().CliError(error.code, str(error), error.data) from None

    monkeypatch.setattr(cli_support, "_daemon_request", request)
    result = CliRunner().invoke(cli_root_app, arguments, input=stdin)
    result.sent = sent
    return result


def cli_json(app: Any, monkeypatch: pytest.MonkeyPatch, arguments: list[str], *, stdin: str | None = None,
             exit_code: int = 0) -> dict:
    result = cli(app, monkeypatch, [*arguments, "--json"], stdin=stdin)
    assert result.exit_code == exit_code, (result.stdout, result.stderr)
    for secret in (CLI_SECRET, GUI_SECRET):
        assert secret not in result.stdout and secret not in result.stderr
    payload = json.loads(result.stdout)
    payload["_sent"] = result.sent
    return payload


def compose(world: World, app: Any, agent: str = "worker") -> Any:
    """The daemon's managed-worker launch composition for one agent of the world registry."""
    channel = build_worker_channel(actor=world.agent.actor, session_ref="sess-1", hyprial_home=world.home,
                                   state_dir=world.state, node_id="pk-node", owner=OWNER)
    spec = HarnessLaunchSpec(harness="pi", name=agent, headless=True)
    return compose_daemon_worker_launch(registry=world.registry, hyprial_home=world.home, spec=spec,
                                        channel=channel, environ={"PATH": "/usr/bin:/bin"}, app=app)


def wire(app: Any, world: World, signer: Any) -> None:
    """Point an app at the world's owner space with a signer and a verified anchor for it."""
    app._user_key_signer = signer
    app._provider_key_space_resolver = storage.FixedSpaceResolver(world.fs, world.space_id)
    app._provider_key_owner_key_port = Anchor(signer)
    app._provider_key_clock = lambda: world.clock[0]


def open_daemon(home: Path, state_dir: Path) -> DaemonApplication:
    """A real DaemonApplication (no sockets, inbox or harnesses); the caller closes it."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    application = DaemonApplication(state_dir=state_dir, socket_path=state_dir / "d.sock", node_id="pk-node",
                                    owner=OWNER, user_id=UNIT_USER_ID, hyprial_home=home, **daemon_dependencies())
    application._inbox = _Unused()  # type: ignore[assignment]
    application._harnesses = _Harnesses()  # type: ignore[assignment]
    application._presence = _Unused()  # type: ignore[assignment]
    return application


def owner_daemon(world: World, state_dir: Path) -> DaemonApplication:
    """A real daemon on the world's home wired with fresh signer/anchor/resolver objects."""
    application = open_daemon(world.home, state_dir)
    wire(application, world, bound_signer(world.state))
    return application


def close_daemon(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()


def legacy_user_grant(world: World, grant_id: str, entry_id: str) -> None:
    """A pre-existing legacy user-provider grant and its plaintext entry, as the old path stores them."""
    from hyprial.identity import SecretResolver, SecretSource

    SecretResolver(world.home, world.registry).write_user_provider(entry_id, {"key": LEGACY_SECRET})
    world.registry.grant_secret("worker", grant_id=grant_id, source=SecretSource.USER_PROVIDER,
                                entry_id=entry_id, field_name="key", environment_names=(ENV,), revision=1)
