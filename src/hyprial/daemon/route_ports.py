"""Typed route-registration commands and committed completion events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from hyprial.contracts.ports import CommandSink

from .lifecycle_receipts import MutationProvenance


@dataclass(frozen=True, slots=True)
class RouteSpec:
    route_id: str
    liveliness_key: str
    inbox_key: str
    advertise: bool = True


@dataclass(frozen=True, slots=True)
class EnsureRouteCommand:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    spec: RouteSpec
    owner_lease: str | None = None


@dataclass(frozen=True, slots=True)
class DropRouteCommand:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    route_id: str
    owner_lease: str | None = None


RouteCommand: TypeAlias = EnsureRouteCommand | DropRouteCommand
RouteCommandSink: TypeAlias = CommandSink[RouteCommand]


@dataclass(frozen=True, slots=True)
class RouteMutationCompleted:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    operation: str
    route_id: str
    changed: bool
    provenance: MutationProvenance


@dataclass(frozen=True, slots=True)
class RouteMutationFailed:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    operation: str
    route_id: str
    code: str
    detail: str


RouteEvent: TypeAlias = RouteMutationCompleted | RouteMutationFailed
