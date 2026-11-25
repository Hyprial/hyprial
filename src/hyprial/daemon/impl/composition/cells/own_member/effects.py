"""An agent's own cell member: the one harness ``lifecycle.start`` runs (I6-5).

Decision notes:

- Critic P0 (one harness per agent): an agent's own member (the document
  member that is the record's URI) runs as the one harness
  ``lifecycle.start`` launches, ``<harness>:<actor>``, from the desired-state
  spec (the document's ``model`` applied).  Effects for it never launch a
  second harness under the URI: ENSURE adopts the running harness when its
  live configuration digest matches and, for a policy, its recorded ready
  frame acknowledged the desired policy digest (``action=adopt``);
  otherwise ENSURE and POLICY_APPLY restart that same harness through
  ``_ipc_down`` + ``_ipc_lifecycle_start`` (the restart recipe of the
  deleted authority service) and verify the acknowledgement.  A failed
  apply relaunches the previous spec; a raise after the stop takes the same
  rollback branch.  STOP downs it; the captured spec lets a following
  ENSURE relaunch it after ``down`` removed the desired-state row.  Policy
  keys are ``launch_key`` of that
  spec, so a lifecycle, restore or crash-restart launch reads the policy
  reconcile published.  ``nudge_member_reconcile`` re-runs reconcile for an
  agent's cell after a launch without composing anything.  A document
  ``kind`` that differs from the running harness fails the effect
  (``OWN_MEMBER_HARNESS_MISMATCH``) instead of launching a second harness:
  switching harness is an ``agent start --harness`` decision.  Before it
  stops anything, a restart runs the strict-resume preflight for the
  learned session ref; a refusal stops nothing (an apply keeps the previous
  policy running and the cell degrades; an ensure leaves the harness as is).
- Critic V2 P1-A: a restart keeps the agent running when it can.  A restart
  STOP (one carrying the spec it restarts into) stops nothing: the ENSURE
  that follows restarts the harness in place, so the desired-state row is
  never dropped between the two.  ``member_launch_policy`` records the
  policy every launch received; when the new spec fails, the harness is
  relaunched from the spec it ran before the effect (never the document's
  new ``model``) with ``previous`` when the core has one, otherwise with
  exactly the recorded policy, and the failure reports the digest of what
  runs, so the member is degraded, not stopped.  A ``kind`` mismatch is
  checked before anything is stopped.  Only a member that was not running
  stays down after a failed ENSURE.
- Critic V2 P1-B: that relaunch pins the recorded policy for itself only
  and then releases the entry, so a later operator, restore or crash
  launch reads the document again; nothing publishes a sticky "no policy"
  for an ENSURE.  ``own_launch_in_progress`` tells
  ``nudge_member_reconcile`` which launches are reconcile's own: only an
  operator launch rechecks the member, so reconcile's restarts never feed
  back into a loop.
- Critic V3 P1-1: a restart stops the harness with ``keep_intent`` (the
  deactivate keeps the desired-state row), so a failed start or a daemon
  killed between the stop and the start leaves a ``running`` row that boot
  restore relaunches.  A real STOP and an operator stop still remove it.
- I6-5 merged onto dev's I6-2 (2026-11-24): the own member joins the
  reconcile actor's one restart chain instead of restarting on its own.
  Its ENSURE adopts a running harness whose live digest (normalised args,
  and the policy digest the process acknowledged in its ready frame)
  equals the desired one; a running harness that differs is reported as
  an adoption with that live digest, so the actor's bounded
  ``adopted-spec-drift`` restart (or a planned drain-and-restart) issues a
  restart STOP and then an ENSURE.  The restart STOP stops nothing and arms
  the member (the same latch the generic path uses for its drift stop);
  only an armed ENSURE, or one that finds nothing running, runs the
  in-place restart with ``keep_intent``, its preflight and its rollback.
  Stop and start stay inside that one ENSURE because the kind check, the
  resume preflight and the capture of what ran must see both.  A restart
  that settles on a still-different digest is reported unadopted, so the
  actor fails the member instead of looping.  The own member keeps
  refusing a ``kind`` change (``OWN_MEMBER_HARNESS_MISMATCH``), where a
  generic declared member is replaced by the document kind: switching an
  agent's harness stays an ``agent start --harness`` decision.
- Integration critic P2-2, coordinator ruling (2026-11-24): the drift
  restart is bounded once per desired digest, not once per revision.  An
  own ENSURE restart that does not settle on the desired digest records that
  digest (``_drift_exhausted``); a later unarmed ENSURE that finds the
  harness live with a different digest reports it unadopted while the
  desired digest is still the recorded one, so the actor fails the member
  (``config-digest-mismatch``) instead of restarting it on every unrelated
  revision.  A revision that changes the desired digest gets one restart of
  its own; an ENSURE that settles on the desired digest, an adoption that
  matches, or a real STOP clears the record.  An ENSURE that finds nothing
  running still launches (KP-131).  The generic path keeps dev's stricter
  latch (one drift restart per member until an ENSURE settles).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from hyprial.daemon.impl.cells.reconcile import (
    DesiredMemberSpec,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
)
from hyprial.daemon.impl.cells.reconcile.policy import (
    LaunchPolicy,
    MemberLaunchPolicies,
    PolicyKey,
    launch_key,
)
from hyprial.daemon.impl.composition.cells.member_rows import (
    _agent_for_id,
    _matching_desired_spec,
)


POLICY_UNAVAILABLE_EVENT = "cell.member_policy_unavailable"
OWN_HARNESS_MISMATCH = "OWN_MEMBER_HARNESS_MISMATCH"
POLICY_APPLY_STARTED_EVENT = "cell.member_policy_apply_started"
POLICY_APPLIED_EVENT = "cell.member_policy_applied"
POLICY_APPLY_FAILED_EVENT = "cell.member_policy_apply_failed"


@dataclass(frozen=True, slots=True)
class _OwnHarness:
    """The one harness ``lifecycle.start`` runs for an agent's own member."""

    actor: str
    harness_id: str
    spec: Any
    running_spec: Any
    key: PolicyKey


@dataclass(frozen=True, slots=True)
class _Before:
    """What an own member ran before an effect touched it."""

    running: bool
    known: bool
    launch: LaunchPolicy | None


class OwnMemberEffects:
    """Own-member effects of ``AppMemberEffectPort`` (mixed into it)."""

    def own_launch_in_progress(self, actor: str) -> bool:
        """Whether reconcile itself is running ``lifecycle.start`` for *actor*."""

        with self._lock:
            return actor in self._own_launching

    def _own_harness(self, intent: MemberEffectIntent) -> _OwnHarness | None:
        agent = _agent_for_id(self._app, intent.agent_id)
        member = intent.fence.member
        if agent is None or member != str(getattr(agent, "uri", "")):
            return None
        if not (
            callable(getattr(self._app, "_ipc_lifecycle_start", None))
            and callable(getattr(self._app, "_ipc_down", None))
        ):
            return None
        found = None
        loader = getattr(getattr(self._app, "desired_state", None), "load", None)
        if callable(loader):
            try:
                found = _matching_desired_spec(tuple(loader().harnesses), agent, member)
            except Exception:  # noqa: BLE001 - unreadable desired state uses the captured spec
                found = None
        if found is not None and (getattr(found, "member", None) or found.name) != agent.actor:
            return None
        cache_key = (intent.agent_id, member)
        with self._lock:
            if found is None:
                found = self._own_specs.get(cache_key)
            if found is not None:
                self._own_specs[cache_key] = found
        if found is None:
            return None
        target = found
        spec = intent.spec
        if (
            spec is not None
            and spec.harness == found.harness
            and spec.model is not None
            and spec.model != found.model
        ):
            target = replace(found, model=spec.model)
        key = launch_key(found)
        if key is None:
            return None
        return _OwnHarness(
            actor=str(agent.actor),
            harness_id=f"{found.harness}:{getattr(found, 'member', None) or found.name}",
            spec=target,
            running_spec=found,
            key=key,
        )

    def _execute_own(
        self, intent: MemberEffectIntent, harnesses: Any, own: _OwnHarness
    ) -> MemberEffectCompletion:
        policies = self._policies()
        if intent.action is MemberEffectAction.STOP:
            if intent.spec is None:
                if self._own_running(harnesses, own):
                    self._own_down(own)
                if policies is not None:
                    policies.forget(own.key)
                with self._lock:
                    self._drift_exhausted.pop((intent.agent_id, intent.fence.member), None)
            else:
                with self._lock:
                    self._drift_stopped.add((intent.agent_id, intent.fence.member))
            return MemberEffectCompletion(
                fence=intent.fence,
                attempt_id=intent.attempt_id,
                action=intent.action,
                succeeded=True,
            )
        restart_key = (intent.agent_id, intent.fence.member)
        with self._lock:
            armed = restart_key in self._drift_stopped
            self._drift_stopped.discard(restart_key)
            exhausted = self._drift_exhausted.get(restart_key)
        spec = intent.spec
        if spec is None:
            return self._completion(
                intent, None, "MISSING_DESIRED_SPEC", "an own-member effect requires a desired spec"
            )
        if policies is None:
            return self._completion(
                intent, None, "POLICY_LOOKUP_UNAVAILABLE", "no member launch policy lookup"
            )
        previous = intent.previous
        known, launched = policies.launched(own.key)
        before = _Before(self._own_running(harnesses, own), known, launched)
        if spec.harness != own.spec.harness:
            return self._own_kept(
                intent, previous, harnesses, own, before, OWN_HARNESS_MISMATCH,
                f"{own.harness_id} runs {own.spec.harness}; the document names {spec.harness}; "
                "switch harnesses with 'hyprial agent start --harness'",
            )
        if intent.action is MemberEffectAction.ENSURE:
            live = self._own_live(intent, spec, harnesses, own)
            if live is not None and live[0] == spec.config_digest:
                with self._lock:
                    self._drift_exhausted.pop(restart_key, None)
                return self._own_adopt(intent, spec, own, policies, live)
            if live is not None and not armed:
                return MemberEffectCompletion(
                    fence=intent.fence,
                    attempt_id=intent.attempt_id,
                    action=intent.action,
                    succeeded=True,
                    reported_digest=live[0],
                    adopted=exhausted != spec.config_digest,
                )
        else:
            self._policy_event("info", POLICY_APPLY_STARTED_EVENT, intent, spec, previous=previous)
        completion = self._own_cycle(intent, spec, previous, harnesses, own, policies, before)
        with self._lock:
            if completion.reported_digest == spec.config_digest:
                self._drift_exhausted.pop(restart_key, None)
            elif intent.action is MemberEffectAction.ENSURE:
                self._drift_exhausted[restart_key] = spec.config_digest
        return completion

    def _own_live(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        harnesses: Any,
        own: _OwnHarness,
    ) -> tuple[str, int] | None:
        """The live digest over the policy the process acknowledged, and its version."""

        version = intent.fence.revision
        acknowledged_digest = None
        if spec.policy is not None:
            acknowledged_digest, acknowledged = self._ready_ack(own.key)
            version = acknowledged or version
        digest = self._live_digest(
            intent, replace(spec, policy_digest=acknowledged_digest), harnesses, own.harness_id
        )
        return None if digest is None else (digest, version)

    def _ready_ack(self, key: PolicyKey) -> tuple[str | None, int | None]:
        """The policy digest and version a process under *key* acknowledged, if any."""

        policies = self._policies()
        ready = policies.ready_frame(key) if policies is not None else None
        authority = ready.get("authority") if isinstance(ready, Mapping) else None
        if not isinstance(authority, Mapping):
            return None, None
        found = authority.get("policyDigest")
        acknowledged = authority.get("version")
        return (
            found if isinstance(found, str) else None,
            acknowledged if type(acknowledged) is int and acknowledged >= 1 else None,
        )

    def _own_adopt(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        own: _OwnHarness,
        policies: MemberLaunchPolicies,
        live: tuple[str, int],
    ) -> MemberEffectCompletion:
        digest, version = live
        policies.publish(own.key, spec.policy, version=version)
        policies.mark_verified(own.key)
        if spec.policy is not None:
            self._policy_event("info", POLICY_APPLIED_EVENT, intent, spec, action="adopt")
        return self._completion(intent, digest, None, None)

    def _own_cycle(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        previous: DesiredMemberSpec | None,
        harnesses: Any,
        own: _OwnHarness,
        policies: MemberLaunchPolicies,
        before: _Before,
    ) -> MemberEffectCompletion:
        action = "apply" if intent.action is MemberEffectAction.POLICY_APPLY else "ensure"
        refusal = self._own_preflight(own)
        if refusal is not None:
            code, detail = refusal
            self._policy_event("warn", POLICY_APPLY_FAILED_EVENT, intent, spec, previous=previous,
                               code=code, detail=detail, rolledBack=False, action=action,
                               restarted=False)
            return self._own_kept(intent, previous, harnesses, own, before, code, detail)
        code, detail = self._own_launch(intent, spec, harnesses, own, policies)
        if code is None:
            policies.mark_verified(own.key)
            if spec.policy is not None or previous is not None:
                self._policy_event(
                    "info", POLICY_APPLIED_EVENT, intent, spec, previous=previous, action=action
                )
            digest = self._live_digest(intent, spec, harnesses, own.harness_id)
            return self._completion(intent, digest, None, None)
        if previous is not None:
            back_code, back_detail = self._own_launch(
                intent, previous, harnesses, own, policies, restore=True
            )
        elif before.running:
            back_code, back_detail = self._own_relaunch(harnesses, own, policies, before)
        else:
            if self._own_running(harnesses, own):
                self._own_down(own)
            policies.forget(own.key)
            self._policy_event("warn", POLICY_APPLY_FAILED_EVENT, intent, spec,
                               code=code, detail=detail, rolledBack=False, action=action)
            return self._completion(intent, None, code, detail)
        self._policy_event("warn", POLICY_APPLY_FAILED_EVENT, intent, spec, previous=previous,
                           code=code, detail=detail, rolledBack=back_code is None,
                           rollbackCode=back_code, action=action)
        if back_code is not None:
            return self._completion(
                intent, None, code, f"{detail}; rollback failed: {back_detail}"
            )
        if previous is not None:
            policies.mark_verified(own.key)
        return self._own_kept(intent, previous, harnesses, own, before, code, detail)

    def _own_kept(
        self,
        intent: MemberEffectIntent,
        previous: DesiredMemberSpec | None,
        harnesses: Any,
        own: _OwnHarness,
        before: _Before,
        code: str,
        detail: str,
    ) -> MemberEffectCompletion:
        """A failed completion carrying the digest of what still runs, if anything."""

        kept = None
        if self._own_running(harnesses, own):
            ran = previous
            if ran is None and intent.spec is not None:
                ran = replace(
                    intent.spec,
                    policy=None,
                    policy_digest=None if before.launch is None else before.launch.digest,
                )
            if ran is not None:
                kept = self._live_digest(intent, ran, harnesses, own.harness_id)
        return MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=False,
            reported_digest=kept,
            error_code=code,
            error_detail=detail,
        )

    def _own_launch(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        harnesses: Any,
        own: _OwnHarness,
        policies: MemberLaunchPolicies,
        *,
        restore: bool = False,
    ) -> tuple[str | None, str | None]:
        try:
            if self._own_running(harnesses, own):
                self._own_down(own, keep_intent=True)
            restored = policies.restore_verified(own.key) if restore else None
            if (
                not restore
                or spec.policy is None
                or restored is None
                or restored.digest != spec.policy.digest
            ):
                policies.publish(own.key, spec.policy, version=intent.fence.revision)
            started = self._own_start(own, own.running_spec if restore else own.spec)
        except Exception as error:  # noqa: BLE001 - a raise after the stop still takes the rollback branch
            return _error_code(error), str(error)[:300]
        code, detail = self._own_started(harnesses, own, started)
        if code is not None:
            return code, detail
        if spec.policy is not None:
            refusal = self._verify_policy_ack(own.key, spec)
            if refusal is not None:
                return "POLICY_NOT_ACKNOWLEDGED", refusal
        return None, None

    def _own_relaunch(
        self,
        harnesses: Any,
        own: _OwnHarness,
        policies: MemberLaunchPolicies,
        before: _Before,
    ) -> tuple[str | None, str | None]:
        """Relaunch what ran before, with the policy that launch received, then unpin it."""

        try:
            if self._own_running(harnesses, own):
                self._own_down(own, keep_intent=True)
            if before.known:
                policies.pin(own.key, before.launch)
            else:
                policies.release(own.key)
            started = self._own_start(own, own.running_spec)
        except Exception as error:  # noqa: BLE001 - a failed relaunch is reported, never raised
            return _error_code(error), str(error)[:300]
        finally:
            policies.release(own.key)
        return self._own_started(harnesses, own, started)

    def _own_start(self, own: _OwnHarness, launch_spec: Any) -> Any:
        params = dict(launch_spec.to_json())
        params["name"] = own.actor
        params["provider"] = launch_spec.harness
        with self._lock:
            self._own_launching.add(own.actor)
        try:
            return self._app._ipc_lifecycle_start("lifecycle.start", params)
        finally:
            with self._lock:
                self._own_launching.discard(own.actor)

    def _own_started(
        self, harnesses: Any, own: _OwnHarness, started: Any
    ) -> tuple[str | None, str | None]:
        if isinstance(started, Mapping) and started.get("readiness") == "unconfirmed":
            return "MEMBER_NOT_READY", f"{own.harness_id} did not confirm readiness"
        if not self._own_running(harnesses, own):
            return "MEMBER_NOT_RUNNING", f"{own.harness_id} is not running after start"
        return None, None

    def _own_preflight(self, own: _OwnHarness) -> tuple[str, str] | None:
        session_ref = getattr(own.spec, "session_ref", None)
        check = getattr(self._app, "_require_resumable_session", None)
        if not session_ref or not callable(check):
            return None
        try:
            check(own.spec, session_ref)
        except Exception as error:  # noqa: BLE001 - a refused resume stops nothing
            return _error_code(error), str(error)[:300]
        return None

    def _own_down(self, own: _OwnHarness, *, keep_intent: bool = False) -> None:
        self._app._ipc_down(
            {"target": own.actor, "provider": own.spec.harness, "agentsOnly": True},
            keep_intent=keep_intent,
        )

    @staticmethod
    def _own_running(harnesses: Any, own: _OwnHarness) -> bool:
        row = harnesses.actor.read_harness(own.harness_id)
        return bool(row is not None and getattr(row, "running", False))


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else type(error).__name__


__all__ = [
    "OWN_HARNESS_MISMATCH",
    "OwnMemberEffects",
    "POLICY_APPLIED_EVENT",
    "POLICY_APPLY_FAILED_EVENT",
    "POLICY_APPLY_STARTED_EVENT",
    "POLICY_UNAVAILABLE_EVENT",
]
