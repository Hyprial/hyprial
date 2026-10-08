"""DshApiClient: the per-session turn client over the DSH HTTP API."""
from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self


from hyprial.kernel import HarnessLaunchSpec, redact

from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    resolve_turn_timeout_seconds,
)
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.model_provider  import dsh_provider_id, validate_model_selection

if TYPE_CHECKING:
    pass
from hyprial.daemon.impl.harnesses.dsh.api import (
    DSH_REQUEST_TIMEOUT_SECONDS,
    DshApi,
    DshApiError,
    DshWorkerPreset,
    DshWorkerPresetManager,
    DshHttpApi,
)
from hyprial.daemon.impl.harnesses.dsh.worker_home import (
    MANAGED_TURN_TIMEOUT_SECONDS,
    _option_value,
    _positive_float,
)

#: The dsh this client was last verified against end to end.
DSH_VERIFIED_VERSION = "0.1.0-rc.7"

# A provider may return a large or recursively verbose diagnostic.  The worker
# result and its log record share this text, so keep the whole rendered failure
# inside one explicit budget rather than relying on a downstream log rotation.
_DSH_TURN_END_FAILURE_MAX_CHARS = 4096
_DSH_REASON_FIELD_ORDER = {"error": 0, "code": 1, "message": 2, "detail": 3}

@dataclass(slots=True)
class _DshSession:
    session_id: str | None = None

@dataclass(frozen=True, slots=True)
class _DshTurnOutcome:
    result: str
    is_error: bool = False
    failure_code: str | None = None

def _unsupported_dsh_message(process: subprocess.Popen[bytes]) -> str:
    """Why this dsh cannot be driven, with the installed and verified versions."""

    installed = "unknown version"
    try:
        probe = subprocess.run(
            [str(process.args[0]) if isinstance(process.args, (list, tuple)) else "dsh", "--version"],
            capture_output=True,
            text=True,
            timeout=DSH_REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
        installed = (probe.stdout or probe.stderr).strip().splitlines()[0] or installed
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return (
        f"DSH_UNSUPPORTED_VERSION: the installed dsh ({installed}) uses the "
        "newer web API (token login, /api/<service>/<method> routes) that "
        "this hyprial does not support yet; last verified dsh is "
        f"{DSH_VERIFIED_VERSION}. Install it with `npm i -g "
        f"@deepseek-ai/dsh@{DSH_VERIFIED_VERSION}`, or update hyprial once "
        "support for the new dsh ships"
    )

def _events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise DshApiError("DSH session.history returned an invalid value")
    result: list[dict[str, Any]] = []
    for item in value["events"]:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        if isinstance(event, dict):
            result.append(event)
    return result

def _assistant_text(event: dict[str, Any]) -> str | None:
    if event.get("type") != "assistant/message":
        return None
    data = event.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    chunks = [
        item.get("text")
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    text = "\n".join(chunks).strip()
    return text or None


def _turn_end_failure(
    reason: object, *, redact_secrets: Callable[[str], str] | None = None
) -> str:
    """Render one DSH turn ending without dropping its diagnostic fields."""

    kind = reason.get("kind") if isinstance(reason, dict) else None
    prefix = f"DSH turn ended with reason {kind or 'unknown'}"
    detail = (
        {key: value for key, value in reason.items() if key != "kind"}
        if isinstance(reason, dict)
        else {}
    )
    if not detail:
        return prefix
    def diagnostic_order(value: object) -> object:
        if isinstance(value, dict):
            keys = sorted(
                value,
                key=lambda key: (_DSH_REASON_FIELD_ORDER.get(str(key), 4), str(key)),
            )
            return {str(key): diagnostic_order(value[key]) for key in keys}
        if isinstance(value, list):
            return [diagnostic_order(item) for item in value]
        return value

    rendered = json.dumps(
        diagnostic_order(detail),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    generic_redacted = redact(rendered)
    # An explicit check, not an assert: the redaction must hold under -O too.
    rendered = generic_redacted if isinstance(generic_redacted, str) else "[REDACTED]"
    if redact_secrets is not None:
        rendered = redact_secrets(rendered)
    suffix_budget = _DSH_TURN_END_FAILURE_MAX_CHARS - len(prefix) - 2
    if len(rendered) > suffix_budget:
        rendered = rendered[: max(0, suffix_budget - 1)] + "…"
    return f"{prefix}: {rendered}"

class DshApiClient:
    """One persistent DSH conversation exposed as a streaming turn client."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        api: DshApi | None = None,
        session: _DshSession | None = None,
        poll_interval_seconds: float | None = None,
        turn_timeout_seconds: float | None = None,
        worker_channel: WorkerChannel | None = None,
        dsh_home: Path | None = None,
        redact_secrets: Callable[[str], str] | None = None,
    ) -> None:
        self.spec = spec
        # The transport always belongs to the managed process generation that
        # spawned this DSH; only that generation knows the OS-assigned port.
        self.api = api or DshHttpApi("http://127.0.0.1:0")
        self._session = session or _DshSession(_option_value(spec.args, "--session-id"))
        self.agent_preset = _option_value(spec.args, "--agent-preset") or "standard"
        self.poll_interval_seconds = poll_interval_seconds or _positive_float(
            _option_value(spec.args, "--poll-interval"),
            default=0.25,
            label="--poll-interval",
        )
        self.turn_timeout_seconds = turn_timeout_seconds or _positive_float(
            _option_value(spec.args, "--turn-timeout"),
            default=resolve_turn_timeout_seconds(
                spec.turn_timeout_seconds,
                default=MANAGED_TURN_TIMEOUT_SECONDS,
            ),
            label="--turn-timeout",
        )
        self.running = False
        self.exit_error: str | None = None
        self._baseline_seq = 0
        self._turn_pending = False
        # session.cancel was accepted for the pending turn.  Only then may the
        # session's idle readout end the turn (see receive_response).
        self._cancel_sent = False
        # A turn was ended from the idle readout, so its own turn/end may still
        # land late: ignore a turn/end seen before the next turn's turn/start.
        self._stale_turn_end_possible = False
        self.worker_channel = worker_channel
        self.worker_preset: DshWorkerPreset | None = None
        self.dsh_home = Path(dsh_home) if dsh_home is not None else None
        self._redact_secrets = redact_secrets

    @property
    def session_id(self) -> str | None:
        return self._session.session_id

    async def __aenter__(self) -> Self:
        validate_model_selection(
            self.spec.harness, self.spec.model_provider, self.spec.model
        )
        # Readiness has two parts: the banner proves the OS port is bound and
        # the Loader settled, this call proves /api is registered behind the
        # trust fence.  A banner alone is human-facing text, not a contract.
        await self.api.call("host.describe", {})
        if self.worker_channel is not None:
            if self.dsh_home is None:
                raise DshApiError(
                    "per-worker DSH MCP injection requires the worker's DSH_HOME"
                )
            if self._session.session_id is not None:
                raise DshApiError(
                    "a fresh worker MCP identity cannot resume an existing DSH "
                    "session; omit --session-id"
                )
            self.worker_preset = await DshWorkerPresetManager(
                self.api,
                self.worker_channel,
                dsh_home=self.dsh_home,
                base_preset=self.agent_preset,
            ).install()
        if self._session.session_id is None:
            payload: dict[str, object] = {
                "agentPreset": (
                    self.worker_preset.preset_id
                    if self.worker_preset is not None
                    else self.agent_preset
                )
            }
            if self.spec.cwd is not None:
                payload["cwd"] = self.spec.cwd
            value = await self.api.call("session.create", payload)
            if not isinstance(value, dict) or not isinstance(
                value.get("sessionId"), str
            ):
                raise DshApiError("DSH session.create returned no sessionId")
            self._session.session_id = value["sessionId"]
        else:
            # Fail at startup, rather than after the first delivery, when a
            # requested resume target is missing or belongs to another host.
            await self.api.call(
                "session.history",
                {"sessionId": self._session.session_id, "maxMessages": 1},
            )
        if self.spec.model_provider is not None or self.spec.model is not None:
            session_id = self._require_session()
            available = await self.api.call("session.models", {"sessionId": session_id})
            current = available.get("current") if isinstance(available, dict) else None
            if not isinstance(current, dict):
                raise DshApiError("DSH session.models returned no current selection")
            provider = self.spec.model_provider or current.get("provider")
            model = self.spec.model or current.get("model")
            if not isinstance(provider, str) or not provider:
                raise DshApiError("DSH session.models returned no current provider")
            provider = dsh_provider_id(provider)
            if not isinstance(model, str) or not model:
                raise DshApiError("DSH session.models returned no current model")
            value = await self.api.call(
                "session.selectModel",
                {"sessionId": session_id, "provider": provider, "model": model},
            )
            selected = value.get("selected") if isinstance(value, dict) else None
            if not isinstance(selected, dict) or (
                selected.get("provider"), selected.get("model")
            ) != (provider, model):
                raise DshApiError(
                    "DSH session.selectModel did not preserve the requested selection"
                )
        self.running = True
        self.exit_error = None
        return self

    async def __aexit__(self, *args: object) -> bool:
        self.running = False
        if self._turn_pending:
            try:
                await self.interrupt()
            except DshApiError:
                pass
        return False

    async def query(self, prompt: str) -> None:
        session_id = self._require_session()
        history = await self.api.call(
            "session.history", {"sessionId": session_id, "maxMessages": 1}
        )
        self._baseline_seq = max(
            (event.get("seq", 0) for event in _events(history)), default=0
        )
        value = await self.api.call(
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            },
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshApiError("DSH session.prompt was not accepted")
        self._turn_pending = True
        self._cancel_sent = False

    async def receive_response(self) -> AsyncIterator[_DshTurnOutcome]:
        session_id = self._require_session()
        last_text = ""
        idle_after_cancel = False
        while True:
            history = await self.api.call(
                "session.history", {"sessionId": session_id, "maxMessages": 200}
            )
            # Each poll re-reads everything past the baseline, so whether this
            # turn's turn/start has been seen is recomputed per poll.
            turn_started = False
            for event in _events(history):
                seq = event.get("seq")
                if not isinstance(seq, int) or seq <= self._baseline_seq:
                    continue
                if event.get("type") == "turn/start":
                    turn_started = True
                text = _assistant_text(event)
                if text is not None:
                    last_text = text
                if event.get("type") == "turn/end":
                    if self._stale_turn_end_possible and not turn_started:
                        # The previous turn, already ended from the idle
                        # readout, announcing itself late.
                        continue
                    self._stale_turn_end_possible = False
                    self._cancel_sent = False
                    self._turn_pending = False
                    data = event.get("data")
                    reason = data.get("reason") if isinstance(data, dict) else None
                    kind = reason.get("kind") if isinstance(reason, dict) else None
                    if kind == "completed":
                        yield _DshTurnOutcome(last_text)
                    else:
                        yield _DshTurnOutcome(
                            _turn_end_failure(
                                reason, redact_secrets=self._redact_secrets
                            ),
                            True,
                            "HARNESS_TRANSIENT_FAILURE",
                        )
                    return
            if turn_started:
                self._stale_turn_end_possible = False
            if idle_after_cancel:
                # DSH (@deepseek-ai/dsh 0.1.0-rc.7) can honour a cancel that
                # lands as it opens a new step after tool results -- the step
                # ends and the session goes idle -- without ever writing
                # turn/end.  The idle readout was taken on the previous poll
                # and this re-read still has no turn/end, so end the turn here
                # (once); a turn/end that lands later is the stale one above.
                self._cancel_sent = False
                self._turn_pending = False
                self._stale_turn_end_possible = True
                yield _DshTurnOutcome(
                    "DSH turn stopped after cancel without turn/end "
                    "(session idle)",
                    True,
                )
                return
            if self._cancel_sent:
                # State, not a clock: this runs on every poll wake (the loop
                # polls regardless of new events), and only after an accepted
                # session.cancel.
                idle_after_cancel = await self._session_idle(session_id)
                if idle_after_cancel:
                    continue
            # No wall-clock deadline (#277): the turn ends when DSH reports
            # turn/end, or on an explicit interrupt.  Stall reporting waits
            # for a truthful DSH activity source (the current fixed
            # ``_baseline_seq`` re-reads old events every poll and would
            # fake a heartbeat).
            await asyncio.sleep(self.poll_interval_seconds)

    async def _session_idle(self, session_id: str) -> bool:
        """True only when DSH positively reports this session not running.

        An absent session, a missing field or an unreadable reply is not
        idleness: the turn then keeps waiting for turn/end as before.
        """

        value = await self.api.call("session.list", {})
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list):
            return False
        for item in items:
            if isinstance(item, dict) and item.get("sessionId") == session_id:
                return item.get("running") is False
        return False

    async def interrupt(self) -> None:
        if not self._turn_pending:
            return
        await self.api.call("session.cancel", {"sessionId": self._require_session()})
        self._cancel_sent = True

    def force_stop(self) -> None:
        cancel_active = getattr(self.api, "cancel_active", None)
        if callable(cancel_active):
            cancel_active()

    def force_stopped(self) -> bool:
        stopped = getattr(self.api, "stopped", None)
        return bool(stopped()) if callable(stopped) else True

    def _require_session(self) -> str:
        if self._session.session_id is None:
            raise DshApiError("DSH session is not initialized")
        return self._session.session_id
