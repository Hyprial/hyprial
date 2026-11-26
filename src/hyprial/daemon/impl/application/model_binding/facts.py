"""Project saved bindings before the first successful launch, without reading Keys."""

import shutil
from dataclasses import replace

from hyprial.kernel import HarnessLaunchSpec

from .admission import connection_provider, snapshot, status, validate_cell


def launch_row(app, agent, member, row):
    binding = status(app, agent, member).binding
    if binding is None:
        return row
    validate_cell(app, agent, member, binding)
    config = binding.configuration
    if (
        config.auth_kind != "connection"
        or config.harness != "pi"
        or config.mode != "headless"
    ):
        from hyprial.error import codes
        from .admission import refuse

        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option")
    if row is None:
        binary = shutil.which(config.harness)
        cwd = agent.cwd or app._agent_session_domains.agent.resolve_workspace_path(
            agent.actor
        )
        row = HarnessLaunchSpec(
            config.harness,
            agent.actor,
            True,
            cwd=str(cwd),
            command=() if binary is None else (binary,),
            member=agent.actor if member == agent.uri else member,
        )
    return replace(
        row,
        harness=config.harness,
        model=config.model,
        model_provider=connection_provider(config),
        model_binding=snapshot(binding),
    )
