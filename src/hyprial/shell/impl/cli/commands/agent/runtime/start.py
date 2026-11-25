"""``hyprial agent start`` - launch an agent session (headless or interactive).

implB2 (2026-11-02): the plain fresh-headless start (no resume,
replace-interactive, per-invocation runtime args, explicit cwd,
nickname or effort) is the daemon's ``agent.start`` —
one orchestration for the CLI and the GUI.  Everything the declared
``agent.start`` params cannot carry stays on ``lifecycle.start`` below;
interactive/terminal attaches are untouched.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from pathlib import Path
from hyprial.error import codes
import os
import shutil
import sys
import typer

from hyprial.shell.impl.cli.output import warn
from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _resolved_agent_cwd, _resolved_interactive_resume_cwd


def _old_daemon_ignore_refusal(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if code == codes.METHOD_NOT_FOUND:
        return True
    if code != codes.INVALID_ARGUMENT:
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "declared schema",
            "additional propert",
            "ignorestoredharnessargs",
            "unknown field",
            "unexpected field",
        )
    )


def _request_start(
    services: Any,
    method: str,
    params: JsonObject,
    *,
    ignore_stored_args: bool,
    timeout: float | None = None,
) -> Any:
    try:
        return services.runtime.request(
            method,
            params,
            **({"timeout": timeout} if timeout is not None else {}),
        )
    except Exception as error:
        if ignore_stored_args and _old_daemon_ignore_refusal(error):
            data = getattr(error, "data", None)
            raise services.CliError(
                str(getattr(error, "code", codes.INVALID_ARGUMENT)),
                "the daemon does not support --ignore-stored-args; "
                "upgrade the daemon and retry",
                data if isinstance(data, dict) else None,
            ) from error
        raise


@agent_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def start(
    ctx: typer.Context,
    harness_kind: str | None = typer.Argument(None, help="claude, pi, codex, jev, or agy; optional with --tier."),
    name: str = typer.Option(..., "--name"),
    tier: str | None = typer.Option(None, "--tier", help="Select fast, strong, or super; it chooses harness/provider/model, so it cannot be combined with any of them."),
    nickname: str | None = typer.Option(None, "--nickname"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    headless: bool = typer.Option(False, "--headless"),
    replace_interactive: bool = typer.Option(
        False,
        "--replace-interactive",
        help=(
            "With --headless, explicitly stop the same actor's interactive "
            "connector before starting, then keep its --resume session id. "
            "No process is killed by the caller."
        ),
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help=(
            "Resume an existing session by id: interactive claude, pi, and "
            "codex, or headless claude, pi, and codex. Interactive Codex "
            "requires an existing Agent. "
            "jev has no persistent session. "
            "A missing session is refused, never replaced with a fresh one."
        ),
    ),
    model_provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model vendor (for example deepseek); separate from the harness kind.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id."),
    effort: str | None = typer.Option(None, "--effort", help="Reasoning effort: Codex none/minimal/low/medium/high/xhigh/max/ultra; Claude low/medium/high/xhigh/max; Agy low/medium/high. Omitted keeps the model vendor default; model support varies."),
    ignore_stored_args: bool = typer.Option(
        False,
        "--ignore-stored-args",
        help=(
            "One-shot: do not merge stored harnessArgs for this start; "
            "stored values remain unchanged."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start a harness connector through the daemon."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        nonlocal harness_kind, model_provider, model, headless

        from hyprial.daemon import ModelProviderError, validate_model_selection

        from hyprial.kernel import TIERS

        runtime_args = tuple(ctx.args)
        if tier is not None and tier not in TIERS:
            raise services.CliError(codes.INVALID_ARGUMENT, "tier must be fast, strong, or super")
        explicit_selection = (
            harness_kind is not None or model_provider is not None or model is not None
            or any(arg in {"--provider", "--model"} or arg.startswith(("--provider=", "--model="))
                   for arg in runtime_args)
        )
        if tier is not None and explicit_selection:
            # --tier CHOOSES harness/provider/model.  Combined with an explicit
            # one it used to be dropped without a word, starting e.g. a pi with
            # no model vendor or model, silently running the harness's global default
            # (2026-09-21: `start pi --tier super` -> kimi-coding/k3 -> 403).
            # Refuse here, before any daemon call: no agent, no desired state.
            given = [
                label
                for label, present in (
                    (f"harness {harness_kind!r}", harness_kind is not None),
                    ("--provider", model_provider is not None),
                    ("--model", model is not None),
                    (
                        "--provider/--model after '--'",
                        any(
                            arg in {"--provider", "--model"}
                            or arg.startswith(("--provider=", "--model="))
                            for arg in runtime_args
                        ),
                    ),
                )
                if present
            ]
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                f"--tier {tier} chooses harness/provider/model itself; it "
                f"cannot be combined with {', '.join(given)}. Use either "
                f"`hyprial agent start --tier {tier} --name ...` or an explicit harness "
                "with --provider/--model",
            )
        if tier is not None and not explicit_selection:
            # Resolve inside the daemon so the audit uses its own runtime
            # profile, not caller-supplied evidence.  Selection is static
            # configuration, so this is one ordinary IPC round-trip: the
            # request no longer waits on any liveness probe (2026-09-21).
            # Availability recorded by `agent tiers probe` never enters this path.
            # No per-candidate probe budget is passed: the request port applies
            # its single ordinary round-trip default.
            resolved = services.runtime.request(
                "dispatch.matrix.resolve", {"tier": tier, "name": name},
            )
            choice = resolved["selected"]
            harness_kind, model_provider, model = choice["harness"], choice["provider"], choice["model"]
        if harness_kind not in {"claude", "pi", "codex", "jev", "agy"}:
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "harness kind must be claude, pi, codex, jev, or agy (or use --tier without explicit model selection)",
            )
        if harness_kind == "jev":
            if runtime_args:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "jev does not accept positional runtime arguments after '--'",
                )
            if tier is not None or model_provider is not None or model is not None:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "jev model selection belongs to each request; --tier, --provider, and --model are not accepted",
                )
            if resume is not None:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "jev has no persistent session; model selection is per request",
                )
            headless = True
        if harness_kind == "claude":
            from hyprial.shell.impl.cli.commands.agent.runtime.rules.permission_rules import (
                inert_path_rules,
            )

            rule_cwd = _resolved_agent_cwd(name, cwd)
            findings = inert_path_rules(runtime_args, rule_cwd)
            if findings.refuse:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "these claude permission rules would be accepted but would "
                    "not do what they say. A path rule for Write, MultiEdit, "
                    "NotebookEdit or Glob never matches (Edit covers the first "
                    "three, Read covers Glob; to match every use of a tool "
                    "regardless of path, write only the tool name); Edit covers "
                    "every file-modifying tool, so a Write-only restriction "
                    "cannot be expressed with a path rule. In a CLI flag a "
                    f"single leading slash anchors at the working directory "
                    f"({rule_cwd}), so an absolute path needs a double slash: "
                    + "; ".join(f"{old} -> {new}" for old, new in findings.refuse),
                )
            for old, new in findings.warn:
                warn(
                    f"{old} anchors at the working directory ({rule_cwd}), not at "
                    f"the filesystem root; if you meant the absolute path, write {new}",
                    json_output=json_output,
                )
        try:
            encoded_args = validate_model_selection(
                harness_kind, model_provider, model,
                context=f"hyprial agent start --name {name}",
                effort=effort,
                args=runtime_args,
            )
        except ModelProviderError as error:
            raise services.CliError(codes.INVALID_ARGUMENT, str(error)) from error
        if not headless:
            runtime_args = encoded_args
        for option, explicit in (("--provider", model_provider), ("--model", model)):
            if explicit is not None and any(
                value == option or value.startswith(f"{option}=")
                for value in runtime_args
            ):
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    f"{option} was supplied both as a hyprial option and after '--'",
                )
        resolved_cwd = _resolved_agent_cwd(name, cwd)
        resume_agent_before: JsonObject | None = None
        if resume is not None:
            if harness_kind not in {"claude", "pi", "codex"}:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    f"interactive {harness_kind} has no resumable native runtime"
                    if not headless
                    else f"headless {harness_kind} has no resumable native runtime",
                )
            if not resume.strip():
                raise services.CliError(
                    codes.INVALID_ARGUMENT, "--resume requires a session id"
                )
            if harness_kind in {"pi", "codex"} and not headless:
                resolved_cwd = _resolved_interactive_resume_cwd(name, cwd)
        if replace_interactive and not headless:
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "--replace-interactive is only valid with --headless",
            )
        if replace_interactive and resume is None:
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "--replace-interactive requires --resume <session-id>; a switch "
                "must preserve the conversation",
            )
        if not headless:
            from hyprial.daemon import (
                declare,
            )
            from hyprial.kernel import SupportLevel
            from hyprial.kernel import Capability

            attach = declare(harness_kind, headless=False).get(
                Capability.INTERACTIVE_ATTACH
            )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "channel"
            ):
                return services._start_interactive_claude(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    ignore_stored_args=ignore_stored_args,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "extension"
                and harness_kind == "pi"
            ):
                return services._start_interactive_pi(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "app_server"
                and harness_kind == "codex"
            ):
                return services._start_interactive_codex(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                )
            raise services.CliError(
                codes.UNSUPPORTED_CAPABILITY,
                f"{harness_kind} does not support interactive_attach; "
                "use --headless or start an interactive claude session instead",
            )
        if harness_kind != "jev":
            pinned_binary = shutil.which(harness_kind)
            if pinned_binary is None:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    f"{harness_kind} binary not found on PATH; install it "
                    f"(or fix PATH) before starting a {harness_kind} connector",
                )
        if (
            headless
            and resume is None
            and not replace_interactive
            and not runtime_args
            and cwd is None
            and nickname is None
            and effort is None
        ):
            started = _request_start(
                services,
                "agent.start",
                {
                    "name": name,
                    "harness": harness_kind,
                    **(
                        {"provider": model_provider}
                        if model_provider is not None
                        else {}
                    ),
                    **({"model": model} if model is not None else {}),
                    **(
                        {"ignoreStoredHarnessArgs": True}
                        if ignore_stored_args
                        else {}
                    ),
                },
                ignore_stored_args=ignore_stored_args,
                timeout=(
                    LIFECYCLE_OPERATION_DEADLINE_SECONDS
                    + LIFECYCLE_WAIT_MARGIN_SECONDS
                    + LIFECYCLE_IPC_MARGIN_SECONDS
                ),
            )
            if isinstance(started, dict):
                for warning in started.get("warnings", ()):
                    if isinstance(warning, str):
                        warn(warning, json_output=json_output)
            return started
        params: JsonObject = {
            "provider": harness_kind,
            "name": name,
            "headless": True if harness_kind == "jev" else headless,
            "args": list(runtime_args),
            "cwd": str(resolved_cwd),
        }
        if harness_kind == "jev":
            params["command"] = [
                os.path.abspath(sys.executable),
                "-m",
                "hyprial.daemon.impl.harnesses.python_worker._python_worker",
                "--kind",
                "jev",
            ]
        else:
            params["command"] = [os.path.abspath(pinned_binary)]
        if nickname is not None:
            params["nickname"] = nickname
        if model_provider is not None and harness_kind != "jev":
            params["modelProvider"] = model_provider
        if model is not None and harness_kind != "jev":
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        if replace_interactive:
            params["replaceInteractive"] = True
        if ignore_stored_args:
            params["ignoreStoredHarnessArgs"] = True
        if resume is not None:
            # Headless resume: the daemon refuses a session it cannot find
            # (RESUME_SESSION_NOT_FOUND, before anything starts) and one that
            # does not hold (STRICT_RESUME_FAILED, worker stopped).  Without
            # this key the daemon keeps today's default: a fresh session.
            params["sessionRef"] = resume.strip()
            # Native transcript validation is read-only and must precede the
            # reusable Agent write.  A daemon without this dedicated method
            # fails closed with METHOD_NOT_FOUND before agent.create.
            preflight = _request_start(
                services,
                "lifecycle.resume.preflight",
                params,
                ignore_stored_args=ignore_stored_args,
            )
            preflight_snapshot = preflight.get("agentSnapshot")
            if not isinstance(preflight_snapshot, dict):
                raise services.CliError(
                    "INVALID_RESPONSE",
                    "resume preflight did not return the complete Agent snapshot; "
                    "the daemon cannot safely restore a start-time refusal",
                )
        # A5: the Agent exists before the connector does. Resume adds the
        # stricter condition above: an invalid native target cannot rewrite it.
        create_result = services._create_agent_for_start(
            name=name,
            harness=harness_kind,
            runtime="headless",
            cwd=resolved_cwd,
            provider=None if harness_kind == "jev" else model_provider,
            model=None if harness_kind == "jev" else model,
            include_resume_snapshot=resume is not None,
        )
        if resume is not None:
            raw_before = create_result.get("resumePreviousSnapshot")
            if not isinstance(raw_before, dict):
                raise services.CliError(
                    codes.RESUME_AGENT_RESTORE_FAILED,
                    "agent.create did not replace an existing Agent after resume "
                    "preflight; no exact rollback record exists, so inspect the "
                    "new Agent before retrying",
                )
            resume_agent_before = raw_before
        resume_agent_after = create_result.get("resumeSnapshot")
        if resume_agent_before is not None and not isinstance(resume_agent_after, dict):
            raise services.CliError(
                "INVALID_RESPONSE",
                "agent.create did not return the post-write Agent snapshot; "
                "the daemon cannot safely restore a start-time refusal",
            )
        # Harness readiness is bounded by the lifecycle manager's operation
        # deadline, and this wait must OUTLAST the daemon-side wait
        # (deadline + wait margin): a shorter budget abandoned a healthy
        # start with IPC_TIMEOUT while the daemon still owned and settled it
        # (2026-09-14 production).  Same derivation as `down`; the three
        # waits are composed from one deadline, never chosen independently.
        try:
            started = _request_start(
                services,
                "lifecycle.start",
                params,
                ignore_stored_args=ignore_stored_args,
                timeout=(
                    LIFECYCLE_OPERATION_DEADLINE_SECONDS
                    + LIFECYCLE_WAIT_MARGIN_SECONDS
                    + LIFECYCLE_IPC_MARGIN_SECONDS
                    # A resume adds the daemon's readiness check (one margin) and,
                    # when it fails, one undo operation (a full operation wait).
                    + (
                        LIFECYCLE_WAIT_MARGIN_SECONDS
                        + LIFECYCLE_OPERATION_DEADLINE_SECONDS
                        + LIFECYCLE_WAIT_MARGIN_SECONDS
                        if "sessionRef" in params
                        else 0.0
                    )
                ),
            )
        except services.CliError as start_error:
            if (
                resume_agent_before is not None
                and isinstance(resume_agent_after, dict)
                and start_error.code
                in {
                    codes.RESUME_SESSION_NOT_FOUND,
                    codes.STRICT_RESUME_FAILED,
                }
            ):
                # The rollback is a compensating write, not a second start
                # attempt: when it fails its own error must NOT replace the
                # start refusal that made the rollback necessary.  Keep the
                # original code/message as the primary failure and attach the
                # rollback failure as extra context the operator can read.
                try:
                    services.runtime.request(
                        "agent.resume.restore",
                        {
                            "name": name,
                            "expected": resume_agent_after,
                            "replacement": resume_agent_before,
                        },
                    )
                except Exception as restore_error:  # noqa: BLE001 - reported, never mask
                    data = (
                        dict(start_error.data)
                        if isinstance(start_error.data, dict)
                        else {}
                    )
                    data["restoreError"] = {
                        "code": getattr(restore_error, "code", None),
                        "message": str(restore_error),
                    }
                    raise services.CliError(
                        start_error.code, str(start_error), data
                    ) from start_error
            raise
        if resume is not None:
            expected = resume.strip()
            started_session = (
                started.get("sessionRef") if isinstance(started, dict) else None
            )
            if started_session != expected:
                raise services.CliError(
                    codes.STRICT_RESUME_FAILED,
                    f"resume of {harness_kind} session {expected!r} did not hold: "
                    f"the worker established {started_session!r}",
                    {"harness": harness_kind, "name": name, "sessionRef": expected},
                )
        if isinstance(started, dict):
            for warning in started.get("warnings", ()):
                if isinstance(warning, str):
                    warn(warning, json_output=json_output)
        return started

    services.runtime.execute(operation, json_output=json_output)
