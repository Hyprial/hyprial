"""Read-only, switch-gated OrgFS announce attribution events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sized
from typing import Any

from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffect


_Logger = Callable[..., None]


def emit_announce_event(
    enabled: bool,
    logger: _Logger | None,
    event: str,
    *,
    node: str,
    items: Sized | None = None,
    count_field: str = "spaces",
    payload: bytes | None = None,
) -> None:
    """Emit one best-effort event after the single OFF-path boolean check."""

    if not enabled or logger is None:
        return
    fields: dict[str, Any] = {"node": node}
    if items is not None:
        fields[count_field] = len(items)
    if payload is not None:
        fields["bytes"] = len(payload)
    try:
        logger("info", event, **fields)
    except Exception:
        return


def announce_effects(
    enabled: bool, event: str, *, node: str, items: Sized
) -> tuple[_DirectoryEffect, ...]:
    """Create an actor-routed event only when diagnostics are enabled."""

    if not enabled:
        return ()
    return (
        _DirectoryEffect(
            "instrument",
            details=(("event", event), ("node", node), ("spaces", len(items))),
        ),
    )


def emit_announce_effect(
    enabled: bool, logger: _Logger | None, effect: _DirectoryEffect
) -> None:
    """Handle an actor-routed event without letting diagnostics affect flow."""

    if not enabled or logger is None:
        return
    details = dict(effect.details)
    event = str(details.pop("event", ""))
    if not event:
        return
    try:
        logger("info", event, **details)
    except Exception:
        return


class AnnounceInstrumentation:
    """Small mixin that keeps diagnostic mechanics out of OrgFS ownership."""

    _announce_instrumentation_enabled: bool
    logger: _Logger | None

    def _announce_event(
        self, event: str, *, node: str, items: Sized | None = None
    ) -> None:
        emit_announce_event(
            self._announce_instrumentation_enabled,
            self.logger,
            event,
            node=node,
            items=items,
        )

    def _announce_effects(
        self, event: str, *, node: str, items: Sized
    ) -> tuple[_DirectoryEffect, ...]:
        return announce_effects(
            self._announce_instrumentation_enabled, event, node=node, items=items
        )

    def _announce_buffered_effects(
        self,
        node: str,
        items: Sized,
        diagnostics: Iterable[Mapping[str, object]],
    ) -> tuple[_DirectoryEffect, ...]:
        return (
            *self._announce_effects("announce.buffered", node=node, items=items),
            *(
                _DirectoryEffect("log", details=tuple(sorted(fields.items())))
                for fields in diagnostics
            ),
        )

    def _emit_announce_effect(self, effect: _DirectoryEffect) -> None:
        emit_announce_effect(
            self._announce_instrumentation_enabled, self.logger, effect
        )
