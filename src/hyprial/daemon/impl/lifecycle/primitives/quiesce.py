# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3: quiesce as a unit with no ``transfer.*`` params.
#   Collaborators are plain callables so the transfer handler and a teleport
#   service drive the same sequence.
# - Order, owned by ``transfer.quiesce`` before extraction: resolve once to
#   refuse an unknown worker before any node-wide write-back, force-sync the
#   session refs, resolve again so the spec carries the freshest ref, stop that
#   spec, then read the quiescence proof.
# - The proof is produced by an injected port and returned untouched; a proof
#   that is not exactly ``quiescent is True`` is reported as not quiescent and
#   the caller decides what to refuse.
"""Stop an agent after syncing its session refs and report the proof."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import HarnessLaunchSpec


@dataclass(frozen=True, slots=True)
class Quiescence:
    """The spec that was stopped and the proof read afterwards."""

    spec: HarnessLaunchSpec
    proof: Mapping[str, Any]

    @property
    def quiescent(self) -> bool:
        """True only when the proof carries a literal ``True`` verdict."""

        return self.proof.get("quiescent") is True


def quiesce_agent(
    *,
    resolve_spec: Callable[[], HarnessLaunchSpec],
    sync_session_refs: Callable[[], object],
    deactivate: Callable[[HarnessLaunchSpec], object],
    prove_quiescence: Callable[[HarnessLaunchSpec], Mapping[str, Any]],
) -> Quiescence:
    """Resolve, sync refs, re-resolve, stop the fresh spec, then prove it."""

    resolve_spec()
    sync_session_refs()
    spec = resolve_spec()
    deactivate(spec)
    return Quiescence(spec, prove_quiescence(spec))
