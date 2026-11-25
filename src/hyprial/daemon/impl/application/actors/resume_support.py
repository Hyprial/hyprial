# Decision record (file header; no inline comments by repository rule):
# - These three methods were the shared tail of the retired transfer mixin:
#   the pre-start transcript gate and the post-start strict resume that
#   ``lifecycle_ops`` runs for a resumed start, and the container artifact
#   retirement that a removal runs.  They keep their bodies and the
#   application method names that ``lifecycle_ops`` and its tests use.
# - The leftovers log carries ``agent=``, not ``name=``: the logger owns the
#   ``name`` field and refuses it from callers, so passing it crashed instead
#   of reporting leftovers.
"""Resume gates and container retirement shared by the lifecycle operations."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from hyprial.identity import AgentError, AgentHomeError as RegistryHomeError
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors
from hyprial.daemon.impl.lifecycle.primitives import (
    StrictResumeError,
    require_resumable_harness,
    require_resumable_session,
    strict_resume,
)
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation


class _ResumeSupportMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _require_resumable_session(self, spec: HarnessLaunchSpec, session_ref: str) -> None:
        """Refuse a resume whose transcript the harness would not find.

        Checked BEFORE anything starts, in the same HOME the daemon hands its
        children: pi given an unknown ``--session-id`` warns and begins a
        fresh session under that very id, so the post-start id comparison in
        :meth:`_verify_started_resume` passes on a cold start.  For pi this
        file check is the gate that holds.
        """

        require_resumable_harness(spec.harness, spec.headless)
        if spec.cwd is not None:
            cwd = spec.cwd
        else:
            try:
                cwd = self._agent_session_domains.agent.prepare_workspace(spec.name)
            except RegistryHomeError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve default workspace for {spec.name}: {error}",
                ) from error
        runtime_context = None
        agent = self.agents.get(spec.name)
        if agent is not None and agent.config is not None:
            from hyprial.identity import AgentConfigError
            from hyprial.identity import AgentHomeError
            from hyprial.identity import AgentRuntimeError

            try:
                runtime_context = self._agent_session_domains.agent.prepare_runtime_context(
                    agent.actor,
                    spec.harness,
                    cwd,
                    None,
                    containerized=spec.containerized,
                    legacy_reporter=self._report_legacy_agent_home,
                )
            except (
                AgentError,
                AgentConfigError,
                AgentHomeError,
                AgentRuntimeError,
            ) as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve agent-home P2 session root for "
                    f"{spec.harness}:{spec.name}: {error}",
                ) from error
            if runtime_context is None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"agent-home P2 session root is unavailable for "
                    f"{spec.harness}:{spec.name}",
                )
            self._report_agent_runtime_notices(runtime_context)
        require_resumable_session(
            harness=spec.harness,
            name=spec.name,
            headless=spec.headless,
            cwd=cwd,
            session_ref=session_ref,
            home=Path.home(),
            pi_agent_dir=os.environ.get("PI_CODING_AGENT_DIR"),
            runtime_context=runtime_context,
        )

    def _verify_started_resume(
        self, spec: HarnessLaunchSpec, session_ref: str, *, timeout: float
    ) -> str:
        """The started worker must be ON the requested session, or not run.

        The strict resume check shared by start-with-resume operations.  On failure the worker
        is deactivated: an error with a fresh-session worker still running
        behind it under the same name is the one outcome worse than either.
        """

        assert self._harnesses is not None
        try:
            return strict_resume(
                self._harnesses,
                harness=spec.harness,
                name=spec.name,
                member=spec.member,
                session_ref=session_ref,
                timeout=timeout,
            )
        except StrictResumeError as error:
            failure = error
        self._run_lifecycle_operation(
            LifecycleOperation.deactivate(
                f"lifecycle-start-resume-undo:{uuid4().hex}",
                self._lifecycle_spec(spec),
            )
        )
        detail = (
            f"did not become ready within {timeout}s"
            if not failure.ready
            else f"established session {failure.data['established']!r} instead"
        )
        raise DaemonRequestError(
            ipc_errors.STRICT_RESUME_FAILED,
            f"resume of {spec.harness} session {session_ref!r} for {spec.name} "
            f"did not hold: the worker {detail}; it was stopped rather than "
            "left running on a fresh session",
            failure.data,
        ) from failure

    def _retire_container_artifacts(
        self, harness: str, name: str
    ) -> list[str]:
        """Decision D-A: retirement removes containers, the volume, the home.

        Best effort and loud: leftovers come back in the result (and the
        log) instead of masking the operation that triggered the cleanup.
        """

        from hyprial.daemon.impl.transfer.execution import container as xfer_container

        try:
            problems = xfer_container.prune_worker(
                xfer_container.DockerRunner(),
                harness=harness,
                name=name,
                state_dir=self.state_dir,
            )
        except Exception as error:  # noqa: BLE001 - report, never mask
            problems = [str(error)]
        if problems:
            self._log(
                "warn",
                "transfer",
                "container.retire.leftovers",
                harness=harness,
                agent=name,
                leftovers=problems,
            )
        return problems
