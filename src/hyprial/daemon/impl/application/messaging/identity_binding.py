"""Identity binding publication and withdrawal for the daemon (mixin).

Split out of the OrgFS bridge: the binding proof is published to org
directories over OrgFS, but its lifecycle (suppression, retries, the held
proof) is its own concern.
"""

from __future__ import annotations
import json
import math
import threading
import time
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import (
    IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS,
    atomic_json_write,
)
if TYPE_CHECKING:
    pass



_ORGFS_NOTICE_NAMESPACE = "orgfs"




class _BindingPublicationRetry(RuntimeError):
    """Carry the narrowed publication callback across projector retries."""

    def __init__(self, publisher: Callable[[], None]) -> None:
        super().__init__("one or more identity binding publications failed")
        self.retry_publisher = publisher


class _IdentityBindingMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _org_binding_directory(self) -> Any:
        from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore

        return OrgFsDirectoryStore(
            self._require_orgfs_runtime().facade,
            logger=lambda level, domain, event, **fields: self._log(
                level, domain, event, **fields
            ),
        )

    def _load_org_identity_bindings(self) -> tuple[Any, ...]:
        from hyprial.daemon.impl.identity.bindings import OrgBindingCache

        if self.identity_mode != "casdoor":
            return ()
        return OrgBindingCache(self.state_dir).bindings()

    def _start_org_identity_binding_cache(self) -> None:
        if self.identity_mode != "casdoor":
            return
        from hyprial.daemon.impl.identity.bindings import (
            OrgBindingCache,
            OrgBindingCacheProjector,
            OrgBindingSource,
        )
        from hyprial.daemon import resolve_profile  # same-domain public face
        from hyprial.identity import BINDING_ASSERTION_CLIENT_ID

        if getattr(self, "_org_identity_binding_projector", None) is not None:
            return
        directory = self._org_binding_directory()

        def logger(level: str, event: str, **fields: object) -> None:
            self._log(level, "identity", event, **fields)

        # The binding proof's issuer is the network's own login issuer: the
        # resolved profile's issuer, never a literal (a self-hosted profile
        # must verify proofs against its own Casdoor).  The client id is a
        # separate Casdoor app and stays the identity domain's constant.
        profile, _source = resolve_profile(hyprial_home=self.hyprial_home)
        source = OrgBindingSource(
            directory=directory,
            issuer=profile.issuer,
            client_id=BINDING_ASSERTION_CLIENT_ID,
            logger=logger,
        )
        cache = OrgBindingCache(self.state_dir, source=source, logger=logger)
        projector = OrgBindingCacheProjector(
            cache=cache,
            directory=directory,
            facade=self._require_orgfs_runtime().facade,
            logger=logger,
        )
        self._org_identity_binding_projector = projector
        projector.start()

    def _start_org_identity_bindings_safely(self) -> None:
        """Start projection and publication without aborting daemon wiring."""

        try:
            self._start_org_identity_binding_cache()
        except Exception as error:  # noqa: BLE001 - identity cache is additive
            self._log(
                "warn",
                "identity",
                "identity.org-binding.cache-start-failed",
                reason=type(error).__name__,
            )
        try:
            self._publish_identity_binding()
        except Exception as error:  # noqa: BLE001 - publication retries later
            self._log(
                "warn",
                "identity",
                "identity.org-binding.publish-start-failed",
                reason=type(error).__name__,
            )

    def _identity_binding_publication_projector(self) -> Any | None:
        """Return the projector, starting it when publication first needs it."""

        projector = getattr(self, "_org_identity_binding_projector", None)
        if projector is not None:
            return projector
        try:
            self._start_org_identity_binding_cache()
        except Exception as error:  # noqa: BLE001 - retry remains additive
            self._log(
                "warn",
                "identity",
                "identity.org-binding.retry-start-failed",
                reason=type(error).__name__,
            )
            return None
        return getattr(self, "_org_identity_binding_projector", None)

    def _warn_identity_binding_publish_failure(
        self, org: str, error: BaseException
    ) -> None:
        """Rate-limit the org-specific warning emitted by projector retries."""

        now = time.monotonic()
        warnings = getattr(self, "_org_identity_binding_publish_warning_at", None)
        if not isinstance(warnings, dict):
            warnings = {}
            self._org_identity_binding_publish_warning_at = warnings
        previous = warnings.get(org)
        elapsed = now - previous if isinstance(previous, (int, float)) else None
        if elapsed is not None and 0 <= elapsed < (
            IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS
        ):
            return
        warnings[org] = now
        self._log(
            "warn",
            "identity",
            "identity.org-binding.publish-failed",
            org=org,
            reason=type(error).__name__,
        )

    def _identity_binding_suppression(self) -> Any:
        from hyprial.daemon.impl.identity.mutation.suppression import (
            IdentityBindingSuppression,
        )

        suppression = getattr(self, "_org_identity_binding_suppression", None)
        if suppression is None:
            state_dir = getattr(self, "state_dir", self.hyprial_home / "state")
            suppression = IdentityBindingSuppression(state_dir)
            self._org_identity_binding_suppression = suppression
        return suppression

    def _identity_binding_publication_lock(self) -> threading.RLock:
        lock = getattr(self, "_org_identity_binding_publication_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._org_identity_binding_publication_lock = lock
        return lock

    def _add_pending_identity_binding_publication(
        self, orgs: tuple[str, ...] | None
    ) -> None:
        with self._identity_binding_publication_lock():
            if orgs is None:
                self._org_identity_binding_pending_all = True
                self._org_identity_binding_pending_orgs = set()
                return
            if getattr(self, "_org_identity_binding_pending_all", False):
                return
            pending = getattr(self, "_org_identity_binding_pending_orgs", None)
            if not isinstance(pending, set):
                pending = set()
                self._org_identity_binding_pending_orgs = pending
            pending.update(orgs)

    def _take_pending_identity_binding_publication(
        self,
    ) -> tuple[str, ...] | None:
        with self._identity_binding_publication_lock():
            if getattr(self, "_org_identity_binding_pending_all", False):
                self._org_identity_binding_pending_all = False
                self._org_identity_binding_pending_orgs = set()
                return None
            pending = tuple(
                sorted(getattr(self, "_org_identity_binding_pending_orgs", set()))
            )
            self._org_identity_binding_pending_orgs = set()
            return pending

    def _publish_pending_identity_bindings(self) -> None:
        orgs = self._take_pending_identity_binding_publication()
        if orgs == ():
            return
        self._publish_identity_binding(orgs=orgs, projector_owned=True)

    def _retry_identity_binding_publication(
        self,
        orgs: tuple[str, ...] | None,
        *,
        projector_owned: bool,
    ) -> None:
        self._add_pending_identity_binding_publication(orgs)
        publisher = self._publish_pending_identity_bindings
        if projector_owned:
            raise _BindingPublicationRetry(publisher)
        projector = self._identity_binding_publication_projector()
        if projector is not None:
            projector.retry_publish(publisher)
        raise RuntimeError("one or more identity binding publications failed")

    def _defer_identity_binding_publication(
        self, orgs: tuple[str, ...] | None, publish_at: float
    ) -> bool:
        self._add_pending_identity_binding_publication(orgs)
        projector = self._identity_binding_publication_projector()
        if projector is None:
            return False
        projector.schedule_publish(publish_at, self._publish_pending_identity_bindings)
        return True

    @staticmethod
    def _identity_binding_suppression_call(call: Callable[[], None], action: str) -> None:
        try:
            call()
        except ValueError as error:
            raise DaemonRequestError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"identity binding suppression state is invalid; binding {action} made no change",
            ) from error

    def _drop_held_identity_binding(self) -> None:
        """Delete a proof that is still a bearer, preserving expired facts."""

        from hyprial.identity import binding_assertion_publish_after

        path = self.hyprial_home / "settings.json"
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except json.JSONDecodeError as error:
            raise DaemonRequestError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "identity settings are invalid JSON; binding withdrawal made no change",
            ) from error
        if not isinstance(settings, dict):
            return
        row = settings.get("identityBinding")
        proof = row.get("proof") if isinstance(row, dict) else None
        if not isinstance(proof, str):
            return
        try:
            held = time.time() <= binding_assertion_publish_after(proof)
        except ValueError:
            held = True
        if not held:
            return
        settings.pop("identityBinding", None)
        atomic_json_write(path, settings)

    @staticmethod
    def _identity_binding_operator_attribution(
        *, operator_verified: bool, peer_pid: int | None
    ) -> dict[str, object]:
        return {
            "operator": "verified" if operator_verified else "unverified",
            "operatorVerified": operator_verified,
            **({"callerPid": peer_pid} if peer_pid is not None else {}),
        }

    @staticmethod
    def _selected_identity_binding_orgs(
        directory: Any, *, org: str | None, all_orgs: bool
    ) -> tuple[str, ...]:
        joined = tuple(value for value in directory.orgs() if isinstance(value, str))
        if all_orgs:
            return joined
        assert org is not None
        if org not in joined:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"organization {org!r} is not joined on this node",
            )
        return (org,)

    def _withdraw_identity_binding(
        self,
        *,
        org: str | None,
        all_orgs: bool,
        operator_verified: bool = False,
        peer_pid: int | None = None,
    ) -> dict[str, object]:
        """Validate, suppress and delete while excluding any publication."""

        with self._identity_binding_publication_lock():
            directory = self._org_binding_directory()
            selected = self._selected_identity_binding_orgs(directory, org=org, all_orgs=all_orgs)
            suppression = self._identity_binding_suppression()
            # Validate before the first deletion so a corrupt file refuses the
            # whole withdrawal instead of leaving the proof gone and rows kept.
            self._identity_binding_suppression_call(suppression.check, "withdrawal")
            self._drop_held_identity_binding()
            attribution = self._identity_binding_operator_attribution(
                operator_verified=operator_verified, peer_pid=peer_pid
            )
            self._identity_binding_suppression_call(
                lambda: suppression.withdraw(
                    org=org, all_orgs=all_orgs, set_by=attribution
                ),
                "withdrawal",
            )
            for target in selected:
                directory.remove_binding(target, str(self.owner))
            projector = getattr(self, "_org_identity_binding_projector", None)
            if projector is not None:
                projector.refresh()
            result: dict[str, object] = {"orgs": list(selected), "operatorVerified": operator_verified}
            self._log(
                "info",
                "identity",
                "identity.binding.withdrawn",
                orgs=list(selected),
                **attribution,
            )
            return result

    def _publish_identity_binding_explicit(
        self,
        *,
        org: str | None,
        all_orgs: bool,
        operator_verified: bool = False,
        peer_pid: int | None = None,
    ) -> dict[str, object]:
        """Lift only the explicitly named suppression and publish or defer."""

        with self._identity_binding_publication_lock():
            directory = self._org_binding_directory()
            selected = self._selected_identity_binding_orgs(directory, org=org, all_orgs=all_orgs)
            suppression = self._identity_binding_suppression()
            self._identity_binding_suppression_call(
                lambda: suppression.publish(org=org, all_orgs=all_orgs),
                "publication",
            )
            self._publish_identity_binding(orgs=selected)
            attribution = self._identity_binding_operator_attribution(
                operator_verified=operator_verified, peer_pid=peer_pid
            )
            self._log(
                "info",
                "identity",
                "identity.binding.published",
                orgs=list(selected),
                **attribution,
            )
            return {
                "orgs": list(selected),
                "operatorVerified": operator_verified,
            }

    def _publish_identity_binding(
        self,
        *,
        orgs: tuple[str, ...] | None = None,
        projector_owned: bool = False,
    ) -> None:
        """Publish a saved assertion only after it is no longer a bearer."""

        with self._identity_binding_publication_lock():
            self._publish_identity_binding_locked(
                orgs=orgs, projector_owned=projector_owned
            )

    def _publish_identity_binding_locked(
        self,
        *,
        orgs: tuple[str, ...] | None,
        projector_owned: bool,
    ) -> None:
        """Publication body under the withdraw/publish serialization lock."""

        projector = getattr(self, "_org_identity_binding_projector", None)
        if getattr(self, "identity_mode", "casdoor") != "casdoor":
            return
        path = self.hyprial_home / "settings.json"
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._log(
                "warn",
                "identity",
                "identity.org-binding.publish-skipped",
                reason=type(error).__name__,
            )
            if projector is not None:
                projector.refresh()
            return
        row = settings.get("identityBinding") if isinstance(settings, dict) else None
        if not isinstance(row, dict):
            if projector is not None:
                projector.refresh()
            return
        from hyprial.identity import binding_assertion_publish_after

        proof = row.get("proof")
        try:
            if not isinstance(proof, str):
                raise ValueError("binding proof is missing")
            publish_after = binding_assertion_publish_after(proof)
        except ValueError as error:
            self._log(
                "warn",
                "identity",
                "identity.org-binding.publish-skipped",
                reason=str(error),
            )
            if projector is not None:
                projector.refresh()
            return
        now = time.time()
        if now <= publish_after:
            deferred_orgs = tuple(orgs) if orgs is not None else None
            self._defer_identity_binding_publication(
                deferred_orgs, math.nextafter(publish_after, math.inf)
            )
            self._log(
                "info",
                "identity",
                "identity.org-binding.publish-deferred",
                publishAfter=publish_after,
            )
            return
        try:
            directory = self._org_binding_directory()
            targets = tuple(directory.orgs()) if orgs is None else tuple(orgs)
            targets = self._identity_binding_suppression().allowed(targets)
        except Exception as error:  # noqa: BLE001 - retry owns source recovery
            self._retry_identity_binding_publication(
                orgs, projector_owned=projector_owned
            )
            raise AssertionError("unreachable") from error
        if not targets:
            if projector is not None:
                projector.refresh()
            return
        failed = False
        failed_orgs: list[str] = []
        for org in targets:
            try:
                directory.put_binding(org, row)
            except Exception as error:  # noqa: BLE001 - one org must not mask others
                failed = True
                failed_orgs.append(org)
                self._warn_identity_binding_publish_failure(org, error)
            else:
                self._log(
                    "info", "identity", "identity.org-binding.published", org=org
                )
        if projector is not None:
            projector.refresh()
        if failed:
            self._retry_identity_binding_publication(
                tuple(failed_orgs), projector_owned=projector_owned
            )
