"""Provider-auth coordinator: failure in, one login flow and one alert out.

Spec: ``spec-provider-oauth-relogin-alert-2026-09-14.md`` (+ 追加 1).  The
whole feature lives behind four injected seams, so tests run the real state
machine against a real ``UserProfileStore`` on a tmp file, and only stub the
two process boundaries (helper runner, owner notifier):

* classification — ``classification.py``, closed tables;
* marking — the existing ``UserProfile.runtime_capability`` slot, now a
  **diagnostic record only** (2026-09-21): ``dispatch.matrix`` no longer
  filters on it and no dispatch decision reads it, so a marked provider is
  still selected.  It is kept because the restore scan, the doctor view and
  the recovery notification all key on it;
* relogin — ``helper.DeviceLoginRunner`` spawning pi's own login code;
* alerting — ``autoupdate.alert.notify_owner``, the existing owner channel.

Never raises
------------
Same contract as ``autoupdate.alert``: this runs inside the delivery pump's
failure path.  A raise there would turn "a worker's provider auth died" into
"the pump died", which is strictly worse.  Every public method captures and
logs instead.

Episodes and rounds (追加 1)
----------------------------
One *episode* per provider = one broken stretch.  An episode issues at most
``MAX_ROUNDS`` device codes; each expiry starts the next round; after the
last one, exactly one closing notice is sent and the episode goes quiet.
A new episode is started only by (a) the next A-class failure or (b) daemon
restore finding a provider still marked broken with no flow in flight — the
restore re-trigger exists because a broken provider keeps producing failures
and the episode record is what bounds login rounds; without (b) a daemon
restart mid-episode would leave the record behind (2026-09-21: the mark no
longer suppresses dispatch, so it is not what makes failures stop arriving).

Recovery has two detectors, because the owner can fix this two ways:
the helper's own ``login()`` resolving (auto flow), or a manual
``pi → /login`` landing a fresh credential in auth.json while we were not
looking — reconciled on the next trigger before any new flow is started.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from ..dispatch.matrix import TIERS
from ..log import redact
from ..squire.profile import RuntimeCapability, UserProfileStore
from .classification import ProviderFailureClass, classify_provider_failure
from .helper import DeviceCodeAnnouncement, HelperOutcome

if TYPE_CHECKING:
    from hyprial.agents.runtime import AgentRuntimeContext

#: New reason constants beside ``squire.probe``'s REASON_* set.  ``unavailable``
#: requires a reason (profile.py), and the restore scan keys on these two.
REASON_PROVIDER_AUTH_INVALID = "provider-auth-invalid"
REASON_PROVIDER_AUTH_ACCOUNT = "provider-auth-account"

#: 追加 1 ②: codes per episode, then one closing notice and silence.
MAX_ROUNDS = 3

#: S2 cooldown: after an episode exhausts its rounds, a provider is capped at
#: one fresh episode per window.  Online workers keep failing while the mark
#: only blocks NEW dispatch, so without this every third turn failure would
#: open a new episode, a new device code, and a new "自动重登录失败" DM --
#: spam that a broken helper can never clear.  Inside the window a failure
#: only updates the record (doctor-visible) and starts no helper / no DM.
RELOGIN_COOLDOWN_SECONDS = 2 * 3600

#: A credential this close to expiry is treated as dead: pi refreshes under
#: a five-minute floor (ModelRuntimeAuthOverrides.minOAuthValidityMs), and a
#: reconcile that accepts a token about to die would clear the mark into the
#: same failure that set it.
_CREDENTIAL_LIVE_FLOOR_SECONDS = 300


class OwnerNotifier(Protocol):
    def __call__(self, text: str, *, idempotency_key: str) -> Any: ...


class HelperRunner(Protocol):
    def __call__(
        self,
        provider: str,
        on_device_code: Callable[[DeviceCodeAnnouncement], None],
        *,
        stop: threading.Event | None = None,
    ) -> HelperOutcome: ...


class RuntimeHelperFactory(Protocol):
    def __call__(self, context: "AgentRuntimeContext") -> HelperRunner: ...


class RuntimeContextValidator(Protocol):
    def __call__(self, context: "AgentRuntimeContext") -> bool: ...


@dataclass
class _Episode:
    """One provider's broken stretch.  ``round`` counts issued codes."""

    provider: str
    key: str
    episode_id: str
    kind: ProviderFailureClass
    auth_path: Path
    runner: HelperRunner
    mark_capabilities: bool = True
    workers: set[str] = field(default_factory=set)
    round: int = 0
    inflight: bool = False
    closing_sent: bool = False
    failure_alerted: bool = False  # helper-machinery failure, once per episode
    sample: str = ""  # verbatim cause text (追加 2: alerts carry the cause)
    exhausted_at: float | None = None  # when the closing notice went out (S2)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


class ProviderAuthCoordinator:
    """Owns the per-provider state machine.  All public methods never raise."""

    def __init__(
        self,
        *,
        profile_store: UserProfileStore,
        owner_key: str | None,
        notifier: OwnerNotifier,
        helper_runner: HelperRunner,
        auth_path: Path,
        host: str,
        stop: threading.Event,
        clock: Callable[[], float] = time.time,
        logger: Callable[..., None] | None = None,
        runtime_helper_factory: RuntimeHelperFactory | None = None,
        runtime_context_validator: RuntimeContextValidator | None = None,
    ) -> None:
        self._store = profile_store
        self._owner_key = owner_key
        self._notifier = notifier
        self._runner = helper_runner
        self._auth_path = auth_path
        self._host = host
        self._stop = stop
        self._clock = clock
        self._log = logger or (lambda *a, **k: None)
        self._runtime_helper_factory = runtime_helper_factory
        self._runtime_context_validator = runtime_context_validator
        self._lock = threading.RLock()
        self._episodes: dict[str, _Episode] = {}

    # ------------------------------------------------------------------ API

    def handle_turn_failure(
        self,
        failure: str,
        *,
        harness: str,
        provider: str | None,
        model: str | None,
        worker: str,
        runtime_context: "AgentRuntimeContext | None" = None,
    ) -> None:
        """One worker turn failure.  Called from the streaming pump thread."""

        try:
            self._handle_turn_failure(
                harness=harness,
                provider=provider,
                model=model,
                worker=worker,
                failure=failure,
                runtime_context=runtime_context,
            )
        except Exception as error:  # noqa: BLE001 -- never-raises contract
            self._log(
                "provider.auth.coordinator.error",
                provider=provider,
                errorType=type(error).__name__,
            )

    def resume_after_restore(self) -> None:
        """Daemon restore hook: re-open episodes for providers still broken.

        追加 1 ①: the dispatch mark blocks the failures that would re-trigger
        the flow, so restore is the bounded re-trigger point.  A credential
        that came back while the daemon was down is honored first — no flow,
        just the recovery.
        """

        try:
            for provider in self._providers_marked(REASON_PROVIDER_AUTH_INVALID):
                if self._credential_live(provider):
                    self._recover(provider)
                    continue
                with self._lock:
                    episode = self._episodes.get(provider)
                    if episode is not None and episode.inflight:
                        continue
                    if episode is not None and (
                        episode.closing_sent or episode.round >= MAX_ROUNDS
                    ):
                        episode = None  # exhausted record; restore restarts fresh
                    if episode is None:
                        episode = self._new_episode(
                            provider,
                            ProviderFailureClass.RELOGINABLE,
                            key=provider,
                            auth_path=self._auth_path,
                            runner=self._runner,
                            mark_capabilities=True,
                        )
                    # Mark + claim the round under the SAME lock that created
                    # the episode.  The old gap between "episode created" and
                    # "inflight set" (in _start_round) let a concurrent worker
                    # failure see inflight=False and start a second helper for
                    # the same provider (S1).  Re-marking is defensively
                    # idempotent: restore found this provider via the mark, but
                    # a manual probe may have cleared it while the credential
                    # was still dead.
                    self._mark_unavailable(
                        episode.provider, REASON_PROVIDER_AUTH_INVALID
                    )
                    episode.inflight = True
                    episode.round += 1
                self._spawn_round(episode)
        except Exception as error:  # noqa: BLE001 -- never-raises contract
            self._log(
                "provider.auth.coordinator.error",
                errorType=type(error).__name__,
            )

    # ------------------------------------------------------------- internals

    def _handle_turn_failure(
        self,
        *,
        harness: str,
        provider: str | None,
        model: str | None,
        worker: str,
        failure: str,
        runtime_context: "AgentRuntimeContext | None",
    ) -> None:
        kind = classify_provider_failure(failure, provider=provider)
        if kind is ProviderFailureClass.OTHER:
            return
        route_key = None
        auth_path = self._auth_path
        runner = self._runner
        mark_capabilities = True
        if runtime_context is not None:
            if (
                self._runtime_context_validator is None
                or not self._runtime_context_validator(runtime_context)
            ):
                self._log(
                    "provider.auth.runtime-context.rejected",
                    worker=worker,
                    provider=provider,
                )
                return
            if self._runtime_helper_factory is None:
                self._log(
                    "provider.auth.runtime-context.unsupported",
                    worker=worker,
                    provider=provider,
                )
                return
            route_key = (
                f"{runtime_context.actor}:{runtime_context.entity_token}"
            )
            auth_path = runtime_context.roots.native_root / "auth.json"
            runner = self._runtime_helper_factory(runtime_context)
            # RuntimeCapability is owner/provider/model scoped and cannot
            # represent two agent-owned auth roots.  P2 routes stay visible
            # in route-keyed episodes/logs without writing a misleading
            # owner-global mark; P24 may add a durable per-agent status type.
            mark_capabilities = False
        if kind is ProviderFailureClass.ACCOUNT:
            self._handle_account_failure(
                harness=harness, provider=provider, model=model,
                worker=worker, failure=failure,
                episode_key=(
                    f"{route_key}:{harness}:{provider}:{model}"
                    if route_key is not None
                    else f"{harness}:{provider}:{model}"
                ),
                mark_capabilities=mark_capabilities,
            )
            return
        if harness != "pi":
            # DeviceLoginRunner is intentionally Pi-native: sending a Codex
            # or Claude failure to it would refresh the daemon's one Pi
            # auth.json and then falsely report another harness recovered.
            # Provider-specific helpers need an agent/incarnation-bound auth
            # context before they can be enabled.  Until then, fail loudly at
            # the exact combination and tell the owner which native login must
            # be repaired; never fall back to the host or another harness.
            assert provider is not None  # implied by RELOGINABLE
            self._handle_unsupported_relogin(
                harness=harness,
                provider=provider,
                model=model,
                worker=worker,
                episode_key=(
                    f"unsupported:{route_key}:{harness}:{provider}:{model}"
                    if route_key is not None
                    else f"unsupported:{harness}:{provider}:{model}"
                ),
                auth_path=auth_path,
                runner=runner,
                mark_capabilities=mark_capabilities,
            )
            return
        # A class: the mark is per-provider (the credential is), so every
        # TIERS combo on this (harness, provider) is marked, not just the
        # model that happened to fail first.
        assert provider is not None  # implied by RELOGINABLE
        episode_key = (
            f"{route_key}:{provider}" if route_key is not None else provider
        )
        if self._credential_live(provider, auth_path=auth_path):
            # Someone relogged by hand while we were marked.  Reconcile first:
            # no flow, just recovery.
            self._recover(episode_key)
            return
        with self._lock:
            episode = self._episodes.get(episode_key)
            if episode is not None and episode.kind is not ProviderFailureClass.RELOGINABLE:
                episode = None
            elif episode is not None and (
                episode.closing_sent or episode.round >= MAX_ROUNDS
            ):
                # 追加 1 ②: an exhausted record does not block a fresh trigger
                # -- but S2 gates that re-trigger behind a cooldown so a broken
                # helper cannot turn every third turn failure into a new code
                # and a new DM.  Inside the window a failure only updates the
                # record (doctor-visible) and starts no helper.
                if (
                    episode.exhausted_at is not None
                    and self._clock()
                    < episode.exhausted_at + RELOGIN_COOLDOWN_SECONDS
                ):
                    episode.workers.add(worker)
                    self._log(
                        "provider.auth.relogin.cooldown",
                        provider=provider,
                        workers=len(episode.workers),
                        episodeId=episode.episode_id,
                    )
                    return
                episode = None
            if episode is None:
                episode = self._new_episode(
                    provider,
                    ProviderFailureClass.RELOGINABLE,
                    key=episode_key,
                    auth_path=auth_path,
                    runner=runner,
                    mark_capabilities=mark_capabilities,
                )
            episode.workers.add(worker)
            if not episode.sample:
                # The cause rides in the alert (追加 2), but only after the
                # same redactor the log pipeline uses: vendor error text can
                # carry bearer tokens / URLs, and this text bypasses the log.
                episode.sample = (
                    redact(failure.strip().splitlines()[0])[:200]
                    if failure.strip()
                    else "unknown"
                )
            if episode.inflight:
                return
            if episode.mark_capabilities:
                self._mark_unavailable(provider, REASON_PROVIDER_AUTH_INVALID)
            episode.inflight = True
            episode.round += 1
        self._spawn_round(episode)

    def _handle_unsupported_relogin(
        self,
        *,
        harness: str,
        provider: str,
        model: str | None,
        worker: str,
        episode_key: str,
        auth_path: Path,
        runner: HelperRunner,
        mark_capabilities: bool,
    ) -> None:
        with self._lock:
            episode = self._episodes.get(episode_key)
            if episode is not None:
                episode.workers.add(worker)
                return
            episode = self._new_episode(
                provider,
                ProviderFailureClass.RELOGINABLE,
                key=episode_key,
                auth_path=auth_path,
                runner=runner,
                mark_capabilities=mark_capabilities,
            )
            episode.workers.add(worker)
            if model is not None and mark_capabilities:
                self._mark_combo_unavailable(
                    harness, provider, model, REASON_PROVIDER_AUTH_INVALID
                )
        self._notify(
            "⚠️ provider 登录已失效，自动重新登录不支持该 harness\n"
            f"主机: {self._host}\n"
            f"组合: {harness} / {provider} / {model or '-'}\n"
            f"worker: {worker}\n"
            "未启动 Pi 登录 helper，也未读取宿主凭据。请在该 agent 的 "
            f"{harness} 原生凭据根完成获授权登录后重试。",
            idempotency_key=f"provider-auth:unsupported:{episode.episode_id}",
        )
        self._log(
            "provider.auth.relogin.failed",
            harness=harness,
            provider=provider,
            model=model,
            worker=worker,
            reason="unsupported-harness-helper",
            episodeId=episode.episode_id,
        )

    def _handle_account_failure(
        self,
        *,
        harness: str,
        provider: str | None,
        model: str | None,
        worker: str,
        failure: str,
        episode_key: str,
        mark_capabilities: bool,
    ) -> None:
        # B class: entitlement/permission.  A relogin cannot fix it, and the
        # boundary may be per-model (one model dropped from the account), so
        # the mark covers exactly the failing combo, and the alert names the
        # manual path.
        key = episode_key
        with self._lock:
            episode = self._episodes.get(key)
            if episode is not None:
                if not mark_capabilities or model is None or self._combo_account_marked(
                    harness, provider, model
                ):
                    # The account mark is still on record: same broken stretch,
                    # dedup the alert.
                    episode.workers.add(worker)
                    return
                # `squire probe` cleared the mark while the episode key stayed
                # -- this is a fresh failure, so re-alert and re-mark (S4).
                self._episodes.pop(key, None)
            episode = _Episode(
                provider=f"{harness}:{provider}:{model}",
                key=key,
                episode_id=uuid.uuid4().hex[:12],
                kind=ProviderFailureClass.ACCOUNT,
                auth_path=self._auth_path,
                runner=self._runner,
                mark_capabilities=mark_capabilities,
                workers={worker},
            )
            self._episodes[key] = episode
            if model is not None and mark_capabilities:
                self._mark_combo_unavailable(
                    harness, provider, model, REASON_PROVIDER_AUTH_ACCOUNT
                )
        summary = (
            redact(failure.strip().splitlines()[0])[:200]
            if failure.strip()
            else "unknown"
        )
        diagnostic_line = (
            "该组合已标记为不可用(仅用于诊断:派发不会因此跳过)。"
            if mark_capabilities
            else "该 agent 的认证路由已记录失败;未写 owner-global 能力标记。"
        )
        recovery_instruction = (
            "人工处理(账号侧)后运行 `hyprial squire probe` 重新探测即可恢复。"
            if mark_capabilities
            else "请修复该 agent 的账号授权后重试;不修改其他 agent 的状态。"
        )
        self._notify(
            f"⚠️ provider 账号/权限类错误(重新登录无法解决)\n"
            f"主机: {self._host}\n"
            f"组合: {harness} / {provider or '-'} / {model or '-'}\n"
            f"worker: {worker}\n"
            f"错误: {summary}\n"
            f"{diagnostic_line}\n"
            f"{recovery_instruction}",
            idempotency_key=f"provider-auth:account:{episode.episode_id}",
        )
        self._log(
            "provider.auth.account.alerted",
            harness=harness,
            provider=provider,
            model=model,
            worker=worker,
        )

    # ----------------------------------------------------------- A-class flow

    def _new_episode(
        self,
        provider: str,
        kind: ProviderFailureClass,
        *,
        key: str,
        auth_path: Path,
        runner: HelperRunner,
        mark_capabilities: bool,
    ) -> _Episode:
        episode = _Episode(
            provider=provider,
            key=key,
            episode_id=uuid.uuid4().hex[:12],
            kind=kind,
            auth_path=auth_path,
            runner=runner,
            mark_capabilities=mark_capabilities,
        )
        self._episodes[key] = episode
        return episode

    def _spawn_round(self, episode: _Episode) -> None:
        thread = threading.Thread(
            target=self._run_round,
            args=(episode, episode.round),
            name=f"hyprial-provider-auth-{episode.provider}",
            daemon=True,
        )
        thread.start()

    def _run_round(self, episode: _Episode, round_no: int) -> None:
        # The never-raises contract covers this thread too: a round thread
        # that lets an exception escape dies with a pytest-unhandled warning
        # in tests and an undiagnosed silent stop in production.  Everything
        # the round can hit — runner, notifier, store — is captured here.
        try:
            self._run_round_body(episode, round_no)
        except Exception as error:  # noqa: BLE001 -- never-raises contract
            self._log(
                "provider.auth.coordinator.error",
                provider=episode.provider,
                errorType=type(error).__name__,
            )
            with self._lock:
                episode.inflight = False

    def _run_round_body(self, episode: _Episode, round_no: int) -> None:
        provider = episode.provider
        self._log(
            "provider.auth.relogin.round",
            provider=provider,
            round=round_no,
            episodeId=episode.episode_id,
        )

        def on_device_code(announcement: DeviceCodeAnnouncement) -> None:
            self._announce(episode, round_no, announcement)

        try:
            outcome = episode.runner(provider, on_device_code, stop=self._stop)
        except Exception as error:  # noqa: BLE001 -- never-raises contract;
            # a raising runner is a failed round, logged like any other.
            self._log(
                "provider.auth.relogin.failed",
                provider=provider,
                errorType=type(error).__name__,
                episodeId=episode.episode_id,
            )
            outcome = HelperOutcome.FAILED
        if self._stop.is_set() or outcome is HelperOutcome.STOPPED:
            with self._lock:
                episode.inflight = False
            return
        if outcome is HelperOutcome.OK:
            self._recover(episode.key)
            return
        with self._lock:
            episode.inflight = False
            if outcome is HelperOutcome.TIMED_OUT and episode.round < MAX_ROUNDS:
                episode.inflight = True
                episode.round += 1
                spawn = True
            else:
                spawn = False
        if spawn:
            self._spawn_round(episode)
            return
        if outcome is HelperOutcome.TIMED_OUT:
            self._close_episode(episode)
        else:
            # FAILED / SPAWN_ERROR: the login machinery itself broke.  One
            # alert per episode (loud, deduped), then the mark stays and the
            # next trigger decides again.
            with self._lock:
                already = episode.failure_alerted
                episode.failure_alerted = True
                if episode.round >= MAX_ROUNDS and episode.exhausted_at is None:
                    # The cap was hit through a broken helper (not the normal
                    # TIMED_OUT close): start the S2 cooldown so the next
                    # failure cannot open a fresh episode mid-window.
                    episode.exhausted_at = self._clock()
            if not already:
                diagnostic_line = (
                    "该 provider 仍标记为不可用(仅用于诊断:派发不会因此跳过)。"
                    if episode.mark_capabilities
                    else "该 agent 的认证路由仍失败;未写 owner-global 能力标记。"
                )
                self._notify(
                    f"⚠️ provider 自动重登录失败\n"
                    f"主机: {self._host}\n"
                    f"provider: {provider}\n"
                    f"结果: {outcome}\n"
                    f"{diagnostic_line}\n"
                    + (
                        f"手动兜底:在本机运行 `pi`,执行 /login {provider}。"
                        if episode.mark_capabilities
                        else "请为该 agent 的 native root 重新授权后重试。"
                    ),
                    idempotency_key=(
                        f"provider-auth:failed:{episode.episode_id}"
                    ),
                )
            self._log(
                "provider.auth.relogin.failed",
                provider=provider,
                outcome=str(outcome),
                episodeId=episode.episode_id,
            )

    def _announce(
        self,
        episode: _Episode,
        round_no: int,
        announcement: DeviceCodeAnnouncement,
    ) -> None:
        with self._lock:
            if self._episodes.get(episode.key) is not episode:
                # The episode closed while the helper was fetching this code
                # (manual relogin reconciled, or recovery already ran): the
                # code is useless, and announcing it after a recovery notice
                # would read as a new failure.
                return
            workers = sorted(episode.workers)
        if announcement.expires_in_seconds is not None:
            deadline = _utc_now().timestamp() + announcement.expires_in_seconds
            expiry = datetime.fromtimestamp(deadline, UTC).strftime("%H:%M UTC")
            expiry_line = f"有效期至约 {expiry}"
        else:
            expiry_line = "有效期未由服务端给出,请尽快完成"
        diagnostic_line = (
            "该标记仅用于诊断,派发不会因此跳过;授权完成后自动恢复并通知。"
            if episode.mark_capabilities
            else "该 agent 的认证路由独立处理;授权完成后自动恢复并通知。"
        )
        self._notify(
            f"⚠️ provider 认证失效,需要本人重新登录\n"
            f"主机: {self._host}\n"
            f"provider: {episode.provider}\n"
            f"受影响 worker({len(workers)}): {', '.join(workers) or '(无)'}\n"
            f"错误: {episode.sample}\n"
            f"请打开 {announcement.verification_uri}\n"
            f"并输入 code: {announcement.user_code}({expiry_line};"
            f"本轮 {round_no}/{MAX_ROUNDS})\n"
            f"{diagnostic_line}",
            idempotency_key=(
                f"provider-auth:code:{episode.episode_id}:r{round_no}"
            ),
        )
        self._log(
            "provider.auth.relogin.announced",
            provider=episode.provider,
            round=round_no,
            workers=len(workers),
            episodeId=episode.episode_id,
        )

    def _close_episode(self, episode: _Episode) -> None:
        with self._lock:
            if episode.closing_sent:
                return
            episode.closing_sent = True
            if episode.exhausted_at is None:
                episode.exhausted_at = self._clock()
        self._notify(
            f"⚠️ provider {episode.provider} 自动重登录 {MAX_ROUNDS} 轮均未完成,"
            f"已停止自动重发\n"
            f"主机: {self._host}\n"
            f"下次 daemon 重启、或该 provider 再次出现认证失败时,会重新发起。\n"
            + (
                f"手动兜底:在本机运行 `pi`,执行 /login {episode.provider}。"
                if episode.mark_capabilities
                else "请为该 agent 的 native root 重新授权后重试。"
            ),
            idempotency_key=f"provider-auth:closing:{episode.episode_id}",
        )
        self._log(
            "provider.auth.relogin.exhausted",
            provider=episode.provider,
            episodeId=episode.episode_id,
        )

    # --------------------------------------------------------------- marking

    def _combos_for_provider(self, provider: str) -> list[tuple[str, str, str]]:
        combos: list[tuple[str, str, str]] = []
        for candidates in TIERS.values():
            for candidate in candidates:
                if candidate.harness == "pi" and candidate.provider == provider:
                    combo = (candidate.harness, candidate.provider, candidate.model)
                    if combo not in combos:
                        combos.append(combo)
        return combos

    def _mark_unavailable(self, provider: str, reason: str) -> None:
        if self._owner_key is None:
            self._log("provider.auth.mark.skipped", provider=provider,
                      reason="no user profile")
            return
        for harness, prov, model in self._combos_for_provider(provider):
            self._mark_combo_unavailable(harness, prov, model, reason)

    def _mark_combo_unavailable(
        self, harness: str, provider: str, model: str, reason: str
    ) -> None:
        if self._owner_key is None:
            return
        if reason == REASON_PROVIDER_AUTH_INVALID:
            existing = self._current_capability(harness, provider, model)
            if (
                existing is not None
                and existing.status == "unavailable"
                and existing.reason == REASON_PROVIDER_AUTH_ACCOUNT
            ):
                # An account/entitlement (B) judgment is stickier: the OAuth
                # (A) mark must not overwrite it, or an A recovery would later
                # clear a combo the account still cannot use (S3).
                return
        self._store.set_runtime_capability(
            self._owner_key,
            RuntimeCapability(
                harness=harness,
                provider=provider,
                model=model,
                status="unavailable",
                reason=reason,
                detail="provider auth broken; see provider.auth.* log events",
                probed_at=_iso_now(),
            ),
        )

    def _clear_marks(self, provider: str) -> None:
        if self._owner_key is None:
            return
        for harness, prov, model in self._combos_for_provider(provider):
            existing = self._current_capability(harness, prov, model)
            if (
                existing is not None
                and existing.status == "unavailable"
                and existing.reason == REASON_PROVIDER_AUTH_ACCOUNT
            ):
                # Recovery of the OAuth credential must not clear an account
                # (B) mark the credential was never about (S3).
                continue
            self._store.set_runtime_capability(
                self._owner_key,
                RuntimeCapability(
                    harness=harness,
                    provider=prov,
                    model=model,
                    status="available",
                    probed_at=_iso_now(),
                ),
            )

    def _current_capability(
        self, harness: str, provider: str, model: str
    ) -> RuntimeCapability | None:
        if self._owner_key is None:
            return None
        profile = self._store.get(self._owner_key)
        if profile is None:
            return None
        for capability in profile.runtime_capabilities:
            if (
                capability.harness == harness
                and capability.provider == provider
                and capability.model == model
            ):
                return capability
        return None

    def _combo_account_marked(
        self, harness: str, provider: str | None, model: str
    ) -> bool:
        existing = self._current_capability(harness, provider, model)
        return (
            existing is not None
            and existing.status == "unavailable"
            and existing.reason == REASON_PROVIDER_AUTH_ACCOUNT
        )

    def _providers_marked(self, reason: str) -> list[str]:
        if self._owner_key is None:
            return []
        profile = self._store.get(self._owner_key)
        if profile is None:
            return []
        providers: list[str] = []
        for capability in profile.runtime_capabilities:
            if (
                capability.status == "unavailable"
                and capability.reason == reason
                and capability.harness == "pi"
                and capability.provider is not None
                and capability.provider not in providers
            ):
                providers.append(capability.provider)
        return providers

    # -------------------------------------------------------------- recovery

    def _credential_live(
        self, provider: str, *, auth_path: Path | None = None
    ) -> bool:
        """auth.json structure check: oauth credential with time left.

        Reads field names and the expiry number only; token values are never
        touched, logged, or returned.  Missing/unreadable file is simply
        "not live" — the login flow is the answer either way.
        """

        try:
            raw = json.loads((auth_path or self._auth_path).read_text("utf-8"))
        except (OSError, ValueError):
            return False
        record = raw.get(provider)
        if not isinstance(record, dict) or record.get("type") != "oauth":
            return False
        expires = record.get("expires")
        if not isinstance(expires, (int, float)):
            return False
        return expires / 1000 > self._clock() + _CREDENTIAL_LIVE_FLOOR_SECONDS

    def _recover(self, episode_key: str) -> None:
        with self._lock:
            episode = self._episodes.pop(episode_key, None)
        provider = episode.provider if episode is not None else episode_key
        marked = (
            provider in self._providers_marked(REASON_PROVIDER_AUTH_INVALID)
            if episode is None or episode.mark_capabilities
            else False
        )
        if episode is None and not marked:
            # Nothing was broken on record: a stray success signal must not
            # announce a recovery nobody was waiting for (the reconcile path
            # and a late helper outcome can both arrive after recovery).
            return
        if episode is None or episode.mark_capabilities:
            self._clear_marks(provider)
        workers = sorted(episode.workers) if episode is not None else []
        recovery_line = (
            "该 provider 的诊断标记已清除(标记从不影响派发)。"
            if episode is None or episode.mark_capabilities
            else "该 agent 的认证路由已恢复;未改写 owner-global 能力标记。"
        )
        self._notify(
            f"✅ provider {provider} 认证已恢复\n"
            f"主机: {self._host}\n"
            f"受影响 worker({len(workers)}): {', '.join(workers) or '(无)'}\n"
            f"{recovery_line}",
            idempotency_key=(
                f"provider-auth:recovered:"
                f"{episode.episode_id if episode is not None else 'external'}"
            ),
        )
        self._log(
            "provider.auth.recovered",
            provider=provider,
            workers=len(workers),
        )

    # ----------------------------------------------------------------- send

    def _notify(self, text: str, *, idempotency_key: str) -> None:
        outcome = self._notifier(text, idempotency_key=idempotency_key)
        delivered = getattr(outcome, "delivered", None)
        self._log(
            "provider.auth.alert",
            delivered=delivered,
            idempotencyKey=idempotency_key,
        )
