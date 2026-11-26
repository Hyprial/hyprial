"""The agent.start IPC orchestration, split out of ``lifecycle_ops``.

Decision record (implB3, 2026-11-02; revB2 REJECT @ dd52727e49)
- B2-2: ``_ipc_agent_start`` routes every start — fresh and existing — through
  the registered ``agent.create`` authority with ``existing="reuse"`` before
  the lifecycle attempt, restoring the CLI's create-before-start ordering
  (``_create_agent_for_start``): the harness-bound create refuses a served
  agent before its ``_ensure_agent`` mutation, so the running refusal still
  precedes any mutation, and a failed start still leaves the requested
  provider/model and runtime-capability updates applied.  The cwd/command
  composition moved ahead of the create so the record update and the launch
  carry the same cwd; the adapter, user-proxy and binary-pin preflights stay
  ahead of the create so a refused start leaves no trace.
- B2-3: direct agent-domain calls in this method (``native_actor``,
  ``agents.get/require``, workspace resolution) no longer need their own
  translation: the ``agent.start`` registry binding applies the single legacy
  ``agent.*`` translation (``call_with_legacy_agent_errors``) around the
  whole orchestration.

Decision record (I2, UUID addressing merge into 0.5.5.4)
- A registered agent is resolved through the registry (``agents.get``), so
  every spelling the registry answers — its name, live display name, ``a.``
  id or id URI — reaches the same record, and the record's actor key drives
  the desired-state match, ``agent.create`` and the launch.  Only a name with
  no record goes through ``native_actor``, the single creation gate.
- The module is a one-file subpackage because ``actors/`` is at its direct
  file limit; ``_LifecycleOpsMixin`` inherits this mixin, as
  ``_AgentsRegistryMixin`` inherits ``_AgentAddressAdmissionMixin``.

2026-11-14: explicit cwd overrides prior intent and uses the harness cwd
validator before record mutation. Agent home, identity and credentials stay
put. Once agent.create accepts it, a later launch failure retains the setting.

Pi approve is an explicit directory-bound launch approval, retained with cwd
in desired state for automatic restoration, not a one-shot permission. False
refuses before mutation even with stored --approve; omission preserves legacy
callers. The existing argument merge and Pi project guard remain authoritative.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.adapters.spec import is_adapter_provider
from hyprial.daemon.impl.harnesses.actor.io_port import (
    HarnessCwdMissingError,
    validate_harness_cwd,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_string_param,
    _required_string,
)
from hyprial.identity import RUNTIME_HEADLESS
from hyprial.kernel import DECLARED_HARNESSES, DaemonRequestError, HarnessLaunchSpec, ipc_errors


class _AgentStartMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_agent_start(self, params) -> Any:
        """Background-start one registered agent behind the agent.start IPC name.

        The CLI's fresh-headless orchestration (A5 create, running refusal,
        durable launch intent, lifecycle.start funnel) moves here so the CLI
        and the GUI drive the same daemon path.  Interactive/terminal starts
        and resume/replace flows stay on lifecycle.start: the declared
        agent.start params carry no sessionRef, replaceInteractive
        or per-invocation runtime args.
        """

        from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

        plain_name = _required_string(params.get("name"), "name")
        agent = self.agents.get(plain_name)
        name = agent.actor if agent is not None else self.agents.native_actor(plain_name)
        requested_harness = _optional_string_param(params.get("harness"), "harness")
        requested_provider = _optional_string_param(params.get("provider"), "provider")
        requested_model = _optional_string_param(params.get("model"), "model")
        requested_member = _optional_string_param(params.get("member"), "member")
        requested_cwd = _optional_string_param(params.get("cwd"), "cwd")
        from hyprial.daemon.impl.application.model_binding.admission import pin, status

        binding = (None if agent is None else status(
            self, agent, requested_member if requested_member not in (None, name) else agent.uri,
        ).binding)
        harness = requested_harness or (
            binding.configuration.harness if binding is not None else
            agent.preferred_harness if agent is not None else None
        )
        if harness is None:
            intents = [
                item.harness
                for item in self.desired_state.load().harnesses
                if item.name == name
            ]
            if len(intents) == 1:
                harness = intents[0]
        if harness is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "agent.start needs a harness: pass harness, or register a "
                "preferred harness by creating the agent first",
            )
        selected = pin(self, HarnessLaunchSpec(harness, name, True, member=requested_member,
                       model=requested_model, model_provider=requested_provider))
        if "approve" in params:
            if harness != "pi" or not isinstance(params["approve"], bool):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "approve is a Pi-only boolean"
                )
            if params["approve"] is False:
                raise DaemonRequestError(
                    "PI_P2_LAUNCH_UNTRUSTED", "Pi working directory was not approved"
                )
            if requested_cwd is None or not requested_cwd.strip():
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "Pi approval requires explicit cwd"
                )
        if is_adapter_provider(harness):
            raise DaemonRequestError(
                ipc_errors.USE_ADAPTER_COMMAND,
                f"{harness} is an external-platform adapter; use "
                "'hyprial adapter start <name>'",
            )
        if harness not in DECLARED_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"unknown harness {harness!r}; expected one of "
                f"{', '.join(sorted(DECLARED_HARNESSES))}",
            )
        prior = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.harness == harness
                and item.name == name
                and (item.member or item.name) == (requested_member or name)
            ),
            None,
        )
        pinned_binary: str | None = None
        if harness in {"claude", "pi", "codex", "agy"} and (
            prior is None or not prior.command
        ):
            pinned_binary = shutil.which(harness)
            if pinned_binary is None:
                raise DaemonRequestError(
                    ipc_errors.HARNESS_BINARY_MISSING,
                    f"{harness} binary not found on PATH; install it (or fix "
                    f"PATH) before starting a {harness} connector",
                )
        if prior is not None:
            cwd = prior.cwd
            command = list(prior.command) if prior.command else None
        else:
            cwd = agent.cwd if agent is not None and agent.cwd else None
            if harness == "jev":
                command = [
                    os.path.abspath(sys.executable),
                    "-m",
                    "hyprial.daemon.impl.harnesses.python_worker._python_worker",
                    "--kind",
                    "jev",
                ]
            else:
                command = [os.path.abspath(pinned_binary)]
        if requested_cwd is not None:
            if not Path(requested_cwd).is_absolute():
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "cwd must be an absolute directory path"
                )
            try:
                validate_harness_cwd(
                    HarnessLaunchSpec(harness, name, headless=True, cwd=requested_cwd)
                )
            except HarnessCwdMissingError as error:
                raise DaemonRequestError(error.code, str(error)) from error
            cwd = requested_cwd
            if params.get("approve") is True:
                try:
                    cwd = str(Path(requested_cwd).resolve(strict=True))
                except OSError as error:
                    raise DaemonRequestError(
                        "HARNESS_CWD_MISSING", "Approved working directory is unavailable"
                    ) from error
        if cwd is None:
            cwd = self._agent_session_domains.agent.resolve_workspace_path(name)
        create_params: JsonObject = {
            "name": name,
            "existing": "reuse",
            "harness": harness,
            "runtime": RUNTIME_HEADLESS,
            "cwd": str(cwd),
            "member": requested_member or name,
        }
        if binding is None and requested_provider is not None and harness != "jev":
            create_params["provider"] = requested_provider
        if binding is None and requested_model is not None and harness != "jev":
            create_params["model"] = requested_model
        METHOD_REGISTRY.invoke(self, "agent.create", create_params)
        launch: JsonObject = {
            "provider": harness,
            "name": name,
            "headless": True,
            "args": ["--approve"] if params.get("approve") is True else [],
            "cwd": str(cwd),
            "ignoreStoredHarnessArgs": (
                params.get("ignoreStoredHarnessArgs") is True
            ),
        }
        if command:
            launch["command"] = command
        if requested_provider is not None and harness != "jev":
            launch["modelProvider"] = requested_provider
        if requested_model is not None and harness != "jev":
            launch["model"] = requested_model
        if selected.model_binding is not None:
            launch.update(modelBinding=dict(selected.model_binding), model=selected.model,
                          modelProvider=selected.model_provider)
        if requested_member is not None:
            launch["member"] = requested_member
        started = self._ipc_lifecycle_start("lifecycle.start", launch)
        running = isinstance(started, dict) and started.get("readiness") != "unconfirmed"
        warnings = [
            warning
            for warning in (started.get("warnings", ()) if isinstance(started, dict) else ())
            if isinstance(warning, str) and warning
        ]
        return {
            "ok": True,
            "accepted": True,
            "running": bool(running),
            **({"warnings": warnings} if warnings else {}),
        }


__all__ = ["_AgentStartMixin"]
