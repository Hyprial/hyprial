"""Registration of the existing command families on a :class:`CliRuntime`.

This module is the shared wiring point A owns.  A family contributes a Typer
object or an explicit registrar; it never imports the process entry.  The
import block below is the historical command-module order: importing each
module runs its Typer decorators, and the ``add_family`` calls below preserve
the group order.  Both orders feed the top-level help inventory, so they must
stay stable.

The imports are intentionally used for registration side effects, not for
re-export; the process entry exposes only ``main``.
"""
from __future__ import annotations

from typing import Any

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.runtime.composition import CliRuntime

from hyprial.shell.impl.cli.commands.common.root import app as root_app
from hyprial.shell.impl.cli.commands.login import commands as _login_commands  # noqa: F401
from hyprial.shell.impl.cli.commands.meta import commands as _meta_commands  # noqa: F401
from hyprial.shell.impl.cli.commands.socialware.commands import socialware_app
from hyprial.shell.impl.cli.commands.daemon import top as _daemon_top  # noqa: F401
from hyprial.shell.impl.cli.commands.doctor import commands as _doctor_commands  # noqa: F401
from hyprial.shell.impl.cli.commands.host import hosts as _host_hosts  # noqa: F401
from hyprial.shell.impl.cli.commands.message import send as _message_send  # noqa: F401
from hyprial.shell.impl.cli.commands.message import ack as _message_ack  # noqa: F401
from hyprial.shell.impl.cli.commands.message import reply as _message_reply  # noqa: F401
from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.message import read as _message_read  # noqa: F401
from hyprial.shell.impl.cli.commands.message.outbox import outbox_app
from hyprial.shell.impl.cli.commands.message.delivery import delivery_app
from hyprial.shell.impl.cli.commands.message.observe import batch as _observe_batch  # noqa: F401
from hyprial.shell.impl.cli.commands.message.observe import inbox as _observe_inbox  # noqa: F401
from hyprial.shell.impl.cli.commands.message.observe import status as _observe_status  # noqa: F401
from hyprial.shell.impl.cli.commands.message.observe import targets as _observe_targets  # noqa: F401
from hyprial.shell.impl.cli.commands.link.commands import link_app
from hyprial.shell.impl.cli.commands.log import log as _log_command  # noqa: F401
from hyprial.shell.impl.cli.commands.message.observe import trajectory as _observe_trajectory  # noqa: F401
from hyprial.shell.impl.cli.commands.init.onboarding import onboarding_app
from hyprial.shell.impl.cli.commands.init.init import init_app
from hyprial.shell.impl.cli.commands.daemon.commands import daemon_app
from hyprial.shell.impl.cli.commands.daemon import service as _daemon_service  # noqa: F401
from hyprial.shell.impl.cli.commands.service import service_app
from hyprial.shell.impl.cli.commands.agent.runtime import start as _agent_start  # noqa: F401
from hyprial.shell.impl.cli.commands.agent.runtime import down as _agent_down  # noqa: F401
from hyprial.shell.impl.cli.commands.upgrade.commands import upgrade_app
from hyprial.shell.impl.cli.commands.upgrade import autoupdate as _autoupdate  # noqa: F401
from hyprial.shell.impl.cli.commands.daemon import restart as _daemon_restart  # noqa: F401
from hyprial.shell.impl.cli.commands.gui import commands as _gui_commands  # noqa: F401
from hyprial.shell.impl.cli.commands.squire.squire import squire_app
from hyprial.shell.impl.cli.commands.squire.user_proxy import user_proxy_app
from hyprial.shell.impl.cli.commands.agent.admin import (
    agent_app,
    config_app as agent_config_app,
    home_app,
    migration_app,
    secret_app,
)
from hyprial.shell.impl.cli.commands.agent import config_rebind as _agent_config_rebind  # noqa: F401
from hyprial.shell.impl.cli.commands.agent import create as _agent_create  # noqa: F401
from hyprial.shell.impl.cli.commands.agent import grant as _agent_grant  # noqa: F401
from hyprial.shell.impl.cli.commands.agent import home as _agent_home  # noqa: F401
from hyprial.shell.impl.cli.commands.agent import restore as _agent_restore  # noqa: F401
from hyprial.shell.impl.cli.commands.agent import secret as _agent_secret  # noqa: F401
from hyprial.shell.impl.cli.commands.agent.provider import provider_app
from hyprial.shell.impl.cli.commands.agent.runtime.tiers import tiers_app
from hyprial.shell.impl.cli.commands.user.commands import user_app
from hyprial.shell.impl.cli.commands.adapter.admin import adapter_app, scopes_app
from hyprial.shell.impl.cli.commands.adapter import authorize as _adapter_authorize  # noqa: F401
from hyprial.shell.impl.cli.commands.adapter import pins as _adapter_pins  # noqa: F401
from hyprial.shell.impl.cli.commands.adapter.routes import route_app
from hyprial.shell.impl.cli.commands.adapter.data import identities_app, media_app
from hyprial.shell.impl.cli.commands.routine.commands import routine_app
from hyprial.shell.impl.cli.commands.workflow.run import workflow_app
from hyprial.shell.impl.cli.commands.host import host_app
from hyprial.shell.impl.cli.commands.service import network as _service_network  # noqa: F401
from hyprial.shell.impl.cli.commands.profile.commands import profile_app
from hyprial.shell.impl.cli.commands.adapter.lark_auth import user_auth_app
from hyprial.shell.impl.cli.commands.config.settings import config_app
from hyprial.shell.impl.cli.commands.fs.commands import fs_app
from hyprial.shell.impl.cli.commands.org.commands import org_app
from hyprial.shell.impl.cli.commands.work import work_app
from hyprial.shell.impl.cli.commands.transfer import (
    TransferCliDependencies,
    register_transfer_commands,
)
from hyprial.shell.impl.cli.commands.agent import order_agent_commands


_ASSEMBLED = False


def _transfer_dependencies(runtime: CliRuntime) -> Any:
    """Build the transfer family's explicit dependency object."""

    from hyprial.shell.impl.cli.commands.common.services import get_services
    from hyprial.shell.impl.cli.commands.transfer.move import parse_depends

    services = get_services()
    return TransferCliDependencies(
        execute=runtime.execute,
        daemon_request=runtime.request,
        state_dir=services._state_dir,
        error_type=services.CliError,
        configured_home=services.configured_hyprial_home,
        default_home=services.default_hyprial_home,
        print=services.print,
        parse_depends=parse_depends,
    )


def build_app(runtime: CliRuntime) -> typer.Typer:
    """Wire every existing family into ``runtime.app`` exactly once.

    This is the process composition lifecycle: the root app is the module-level
    Typer object that command decorators already populate, and ``build_app``
    attaches the families to it.  An independent ``CliRuntime`` must not call
    this function with its own empty app; use ``CliRuntime.add_registrar`` for
    focused family tests.  A second assembly of the process root is rejected
    rather than silently duplicating mutable registration state.

    B supplies move's handler/registrar and replaces the visible move command;
    this function only wires real registrars and never adds an unimplemented
    placeholder.
    """

    global _ASSEMBLED

    if _ASSEMBLED:
        raise RuntimeError("CLI app has already been composed")
    if runtime.app is not root_app:
        raise ValueError(
            "build_app composes the process root app; use "
            "CliRuntime.add_registrar for an independent app"
        )

    runtime.add_family(secret_app, name="secret", parent=agent_app)
    runtime.add_family(migration_app, name="migrate", parent=home_app)
    runtime.add_family(home_app, name="home", parent=agent_app)
    runtime.add_family(agent_config_app, name="config", parent=agent_app)
    runtime.add_family(tiers_app, name="tiers", parent=agent_app)
    runtime.add_family(provider_app, name="provider", parent=agent_app)
    runtime.add_family(adapter_app, name="adapter")
    runtime.add_family(daemon_app, name="daemon")
    runtime.add_family(upgrade_app, name="upgrade", parent=daemon_app)
    runtime.add_family(service_app, name="service", parent=org_app)
    runtime.add_family(squire_app, name="squire")
    runtime.add_family(socialware_app, name="socialware")
    runtime.add_family(user_proxy_app, name="proxy", parent=user_app)
    runtime.add_family(message_app, name="message")
    runtime.add_family(link_app, name="link", parent=agent_app)
    runtime.add_family(config_app, name="config")
    runtime.add_family(agent_app, name="agent")
    runtime.add_family(org_app, name="org")
    runtime.add_family(work_app, name="work", parent=org_app)
    runtime.add_family(fs_app, name="fs", parent=org_app)
    runtime.add_family(profile_app, name="profile")
    runtime.add_family(host_app, name="host")

    transfer_runtime_app = group(
        help="Probe an explicitly supplied transfer runtime without starting an agent.",
        no_args_is_help=True,
    )
    agent_transfer_app = group(
        help=(
            "Protocol-level worker transfer commands driven by the two daemons and "
            "scripts; use `agent move` as the operator entry point."
        ),
        no_args_is_help=True,
    )
    runtime.add_family(transfer_runtime_app, name="runtime", parent=agent_transfer_app)
    runtime.add_family(agent_transfer_app, name="transfer", parent=agent_app, hidden=True)
    runtime.add_family(workflow_app, name="workflow")
    runtime.add_family(routine_app, name="routine")
    runtime.add_family(onboarding_app, name="onboarding", parent=init_app)
    runtime.add_family(init_app, name="init")
    runtime.add_family(route_app, name="route", parent=adapter_app)
    runtime.add_family(identities_app, name="identities", parent=adapter_app)
    runtime.add_family(user_app, name="user")
    runtime.add_family(media_app, name="media", parent=adapter_app)
    runtime.add_family(scopes_app, name="scopes", parent=adapter_app)
    runtime.add_family(user_auth_app, name="user-auth", parent=adapter_app)
    runtime.add_family(outbox_app, name="outbox", parent=message_app)
    runtime.add_family(delivery_app, name="delivery", parent=message_app)

    register_transfer_commands(
        agent_app,
        agent_transfer_app,
        transfer_runtime_app,
        lambda: _transfer_dependencies(runtime),
    )
    order_agent_commands()
    _ASSEMBLED = True
    return runtime.app
