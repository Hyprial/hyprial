"""One explicit configuration, read-back or cancellation through daemon requests."""

from collections.abc import Mapping

import typer

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult

model_app = group(help="Configure a saved model connection; saving does not start the agent.",
                  no_args_is_help=True)


def render(data):
    binding, operation = data.get("binding"), data.get("operation")
    return "\n".join((
        f"binding revision: {binding['bindingRevision'] if binding else 'none'}",
        f"operation: {operation['operationId'] if operation else 'none'}",
        f"configuration state: {operation['phase'] if operation else 'unconfigured'}",
        "process activation: not verified; use agent status/start separately",
    ))


def execute(request, json_output):
    services = get_services()

    def operation():
        result = request()
        if not isinstance(result, Mapping):
            raise services.CliError(codes.INVALID_RESPONSE, "Model operation returned no object.")
        return CliResult(dict(result), render=render)

    services.runtime.execute(operation, json_output=json_output)


def target(name, member, operation_id=None):
    return {"name": name, **({"member": member} if member is not None else {}),
            **({"operationId": operation_id} if operation_id is not None else {})}


@model_app.command("configure")
def configure(
    name: str = typer.Argument(...),
    model: str = typer.Option(..., "--model", help="Exact model ID; never inferred or substituted."),
    connection: str = typer.Option(..., "--connection", help="Saved connection reference, not a Key."),
    config_revision: int = typer.Option(..., "--config-revision"),
    binding_revision: int = typer.Option(..., "--binding-revision", help="Current binding revision; 0 for first configuration."),
    cell_revision: int = typer.Option(..., "--cell-revision", help="Current agent Cell revision."),
    operation_id: str = typer.Option(..., "--operation-id", help="Unique ID; retry the same ID and exact input after read-back."),
    harness: str = typer.Option("pi", "--harness"),
    mode: str = typer.Option("headless", "--mode"),
    member: str | None = typer.Option(None, "--member"),
    json_output: bool = typer.Option(False, "--json"),
):
    params = {
        **target(name, member, operation_id), "expectedBindingRevision": binding_revision,
        "expectedCellRevision": cell_revision, "configuration": {
            "authKind": "connection", "harness": harness, "mode": mode, "model": model,
            "connectionRef": connection, "configRevision": config_revision,
        },
    }
    execute(lambda: get_services().runtime.request("agent.model.configure", params), json_output)


@model_app.command("status")
def status(
    name: str = typer.Argument(...),
    member: str | None = typer.Option(None, "--member"),
    operation_id: str | None = typer.Option(None, "--operation-id"),
    json_output: bool = typer.Option(False, "--json"),
):
    params = target(name, member, operation_id)
    execute(lambda: get_services().runtime.request("agent.model.status", params), json_output)


@model_app.command("cancel")
def cancel(
    name: str = typer.Argument(...),
    operation_id: str = typer.Option(..., "--operation-id"),
    binding_revision: int = typer.Option(..., "--binding-revision"),
    member: str | None = typer.Option(None, "--member"),
    json_output: bool = typer.Option(False, "--json"),
):
    params = {**target(name, member, operation_id), "expectedBindingRevision": binding_revision}
    execute(lambda: get_services().runtime.request("agent.model.cancel", params), json_output)
