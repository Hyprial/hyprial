"""``hyprial pac`` — PAC v2 command group (graph file + flag reactor, slice 1).

All pac surface lives in this module so the tripwire (concept §5's second
headline acceptance) has one exact scope: nothing under ``src/hyprial/pac``
may read an inbox body or reply text — the reactor's input is
``flag_events`` only, and the one daemon call this module makes is the
outbound ``message.send`` (the same wire ``hyprial send`` rides).

State root resolution mirrors the CLI's own isolation boundary
(``HARNESS_STATE_DIR`` else ``<HYPRIAL_HOME>/state``); the database is
``pac-graph.sqlite3``, a sibling of — and strictly separate from — v1's
``workflows.sqlite3``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from time import sleep
from typing import Any

import typer

from hyprial.home import HYPRIALHomeNotInitialized, require_initialized_hyprial_home

from .errors import PAC_GRAPH_NOT_FOUND, PacError
from .context import node_context
from .graph import (
    add_edge,
    add_node,
    activate_graph,
    create_graph,
    close_graph,
    known_agent_names,
    show_graph,
)
from .projection import Projection, audit
from .reactor import NullSender, PacReactor, planned_to_json
from .store import PacGraphStore, default_database_path

pac_app = typer.Typer(
    help="PAC v2: graph file + flag reactor (edit / react / project).",
    no_args_is_help=True,
)
graph_app = typer.Typer(help="Edit the graph file (CAS-versioned structure).", no_args_is_help=True)
flag_app = typer.Typer(help="Flip node flags (owner-only; drives the reactor).", no_args_is_help=True)
notify_app = typer.Typer(help="Notification delivery maintenance.", no_args_is_help=True)
actor_app = typer.Typer(help="Control run-owned actors.", no_args_is_help=True)
debug_app = typer.Typer(help="Internal PAC diagnostics; not a public automation contract.", no_args_is_help=True)
pac_app.add_typer(graph_app, name="graph")
pac_app.add_typer(flag_app, name="flag")
pac_app.add_typer(notify_app, name="notify")
pac_app.add_typer(actor_app, name="actor")
pac_app.add_typer(debug_app, name="debug", hidden=True)


def _state_dir() -> Path:
    configured = os.environ.get("HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    from hyprial.home import configured_hyprial_home

    home, _source = configured_hyprial_home()
    return home / "state"


def _actor_owner() -> str:
    """An acting principal is required; never turn missing identity into denial."""
    from hyprial.daemon.identity import resolve_node_owner
    from hyprial.home import configured_hyprial_home

    home, _source = configured_hyprial_home()
    try:
        return resolve_node_owner(hyprial_home=home)
    except ValueError as error:
        raise PacError("PAC_OWNER_UNKNOWN", str(error)) from error


def _database_path() -> Path:
    return default_database_path(_state_dir())


def _parse_requires(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        document = json.loads(value)
    except ValueError as error:
        raise PacError("PAC_NODE_SHAPE_INVALID", f"--requires must be JSON: {error}") from error
    if not isinstance(document, dict):
        raise PacError("PAC_NODE_SHAPE_INVALID", "--requires must be a JSON object")
    return document


def _emit(document: dict[str, Any], json_out: bool, human: str | None) -> None:
    if json_out:
        typer.echo(json.dumps(document, ensure_ascii=False))
        return
    if human:
        typer.echo(human)
    else:
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))


def _fail(error: PacError, json_out: bool) -> None:
    document = {"ok": False, "code": error.code, "error": str(error)}
    if error.data:
        document["data"] = error.data
    if json_out:
        typer.echo(json.dumps(document, ensure_ascii=False))
    else:
        typer.echo(f"hyprial pac: {error.code}: {error}", err=True)
    raise typer.Exit(code=1)


def _guard(json_out: bool) -> None:
    try:
        require_initialized_hyprial_home()
    except HYPRIALHomeNotInitialized as error:
        if json_out:
            typer.echo(
                json.dumps(
                    {"ok": False, "code": error.code, "error": str(error)},
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(f"hyprial: {error}", err=True)
        raise typer.Exit(code=1) from None


# --------------------------------------------------------------------------- #
# notification delivery port (the outbound-only daemon seam)
# --------------------------------------------------------------------------- #


class DaemonNotificationSender:
    """Deliver one notification through the daemon's public send seam.

    Recipient mapping: an owner that names a registered local agent sends
    to that agent (the daemon resolves the short name); every other owner
    is a person, addressed as ``user:<owner>``.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._agents: set[str] | None = None

    def _recipient(self, owner: str) -> str:
        if self._agents is None:
            self._agents = known_agent_names(self._state_dir)
        return owner if owner in self._agents else f"user:{owner}"

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> str:
        # Imported lazily: hyprial.cli imports this module at registration time,
        # and the daemon-request transport belongs to it.
        from hyprial.cli import _daemon_request

        result = _daemon_request(
            "message.send",
            {
                "from": sender,
                "to": [self._recipient(recipient)],
                "message": text,
                "conversationId": conversation_id,
                "idempotencyKey": idempotency_key,
            },
        )
        deliveries = result.get("deliveries") if isinstance(result, dict) else None
        message_id = None
        if isinstance(deliveries, list):
            for delivery in deliveries:
                if (isinstance(delivery, dict) and delivery.get("messageId")
                        and delivery.get("accepted") is not False and result.get("ok") is not False):
                    message_id = str(delivery["messageId"])
                    break
        if message_id is None:
            # An idempotency key is not an acknowledgement. Leave the outbox
            # pending until the real send seam confirms durable acceptance.
            raise RuntimeError("message.send returned no accepted messageId")
        return message_id


def _reactor(sender: Any | None = None) -> PacReactor:
    store = PacGraphStore(_database_path())
    return PacReactor(store, sender=sender if sender is not None else NullSender())


# --------------------------------------------------------------------------- #
# graph (spec ①: edit with CAS + write-time validation)
# --------------------------------------------------------------------------- #


@graph_app.command("create")
def graph_create(
    name: str = typer.Argument(..., help="Human graph name; the id is minted from it."),
    created_by: str = typer.Option(..., "--by", help="Short name of the creating owner."),
    operation_key: str | None = typer.Option(
        None,
        "--operation-key",
        help="Durable caller identity; replay returns the existing graph.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a graph at version 1."""

    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        head = create_graph(
            store,
            name=name,
            created_by=created_by,
            operation_key=operation_key,
        )
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit({"ok": True, **head}, json_out, f"graph {head['graphId']} at version 1")


@graph_app.command("add-node")
def graph_add_node(
    graph_id: str = typer.Argument(..., help="Graph id (see `pac graph show`)."),
    node_id: str = typer.Argument(..., help="Node id (one workflow step)."),
    owner: str = typer.Option(..., "--owner", help="Owner short name (person or agent)."),
    brief_ref: str = typer.Option(..., "--brief-ref", help="Reference to the node's explanation; PAC stores the reference, never a body."),
    kind: str = typer.Option("task", "--kind", help="Node kind: task, clock, actor, or end."),
    deadline_ms: int | None = typer.Option(None, "--deadline-ms", help="Clock-node deadline in epoch ms."),
    actor_name: str | None = typer.Option(None, "--actor-name", help="Owned actor short name (kind=actor only)."),
    launch_ref: str | None = typer.Option(None, "--launch-ref", help="Launch-spec reference, never its body (kind=actor only)."),
    requires_json: str | None = typer.Option(
        None,
        "--requires",
        help="Capability requirement JSON: currently exactly {tier: fast|strong|super}.",
    ),
    guarded_by_node_id: str | None = typer.Option(
        None,
        "--guarded-by-node",
        help="Existing task node whose completion suppresses this clock's overdue.",
    ),
    expect_version: int = typer.Option(..., "--expect-version", help="Current graph version (CAS); a mismatch refuses the edit."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Add a node under CAS; bumps the graph version by one."""

    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        result = add_node(
            store,
            _state_dir(),
            graph_id=graph_id,
            node_id=node_id,
            owner=owner,
            brief_ref=brief_ref,
            kind=kind,
            deadline_ms=deadline_ms,
            actor_name=actor_name,
            launch_ref=launch_ref,
            requires=_parse_requires(requires_json),
            guarded_by_node_id=guarded_by_node_id,
            expect_version=expect_version,
        )
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit(
        {"ok": True, **result},
        json_out,
        f"node {node_id} added; graph now at version {result['version']}",
    )


@graph_app.command("add-edge")
def graph_add_edge(
    graph_id: str = typer.Argument(..., help="Graph id (see `pac graph show`)."),
    from_node: str = typer.Argument(..., help="Upstream node id."),
    to_node: str = typer.Argument(..., help="Downstream node id."),
    kind: str = typer.Option(
        "forward",
        "--kind",
        help="Edge kind: forward (join edge) or back (declared loop edge).",
    ),
    expect_version: int = typer.Option(..., "--expect-version", help="Current graph version (CAS); a mismatch refuses the edit."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Add an edge under CAS; forward edges must not close an implicit cycle."""

    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        result = add_edge(
            store,
            graph_id=graph_id,
            from_node=from_node,
            to_node=to_node,
            kind=kind,
            expect_version=expect_version,
        )
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit(
        {"ok": True, **result},
        json_out,
        f"edge {result['edge']} ({kind}) added; graph now at version {result['version']}",
    )


@graph_app.command("activate")
def graph_activate(
    graph_id: str = typer.Argument(..., help="Completed draft graph to freeze."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable activation metadata."),
) -> None:
    """Activate a graph as the local principal; structure is then immutable."""
    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        document = activate_graph(store, graph_id, actor=_actor_owner())
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit({"ok": True, **document}, json_out, f"graph {graph_id} active; structure frozen")


@graph_app.command("close")
def graph_close(
    graph_id: str = typer.Argument(..., help="Task/clock graph to close monotonically."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable closure metadata."),
) -> None:
    """Close the run as the local graph owner; this does not change any flag."""
    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        document = close_graph(store, graph_id, actor=_actor_owner())
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit({"ok": True, **document}, json_out, f"graph {graph_id} closed")


@graph_app.command("show")
def graph_show(
    graph_id: str = typer.Argument(..., help="Graph id to restate."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Restate the current version of the whole graph."""

    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        document = show_graph(store, graph_id)
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    human = [
        f"graph {document['graphId']} ({document['name']}) version {document['version']}",
        "nodes:",
        *(
            f"  {node['nodeId']}  owner={node['owner']} kind={node['kind']}"
            f" flag={'set' if node['flag'] else 'unset'}  ref={node['briefRef']}"
            for node in document["nodes"]
        ),
        "edges:",
        *(f"  {edge['from']} -> {edge['to']}  ({edge['kind']})" for edge in document["edges"]),
    ]
    _emit({"ok": True, **document}, json_out, "\n".join(human))


@actor_app.command("stop")
def actor_stop(
    graph_id: str = typer.Argument(..., help="Owning graph id."),
    actor_name: str = typer.Argument(..., help="Owned actor short name."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Request an owner-authorized early stop without changing actor flag intent."""
    from .lifecycle import request_actor_stop

    _guard(json_out)
    store = PacGraphStore(_database_path())
    try:
        request_actor_stop(store, graph_id, actor_name, actor=_actor_owner())
    except PacError as error:
        _fail(error, json_out)
    finally:
        store.close()
    _emit(
        {"ok": True, "graphId": graph_id, "actorName": actor_name, "desired": "down"},
        json_out,
        f"actor {actor_name} stopping",
    )


# --------------------------------------------------------------------------- #
# flags (spec ②③④: the reactor's input)
# --------------------------------------------------------------------------- #


def _flag_command(
    graph_id: str,
    node_id: str,
    actor: str,
    reason_ref: str | None,
    json_out: bool,
    *,
    action: str,
) -> None:
    _guard(json_out)
    reactor = _reactor(DaemonNotificationSender(_state_dir()))
    try:
        if action == "set":
            outcome = reactor.set_flag(graph_id, node_id, actor=actor, reason_ref=reason_ref)
        else:
            outcome = reactor.reset_flag(graph_id, node_id, actor=actor, reason_ref=reason_ref)
    except PacError as error:
        _fail(error, json_out)
    finally:
        reactor.close()
    document = {
        "ok": True,
        "event": outcome.event,
        "notifications": [planned_to_json(item) for item in outcome.planned],
        "delivered": len(outcome.delivered),
        "undelivered": len(outcome.undelivered),
    }
    if outcome.delivery_error:
        document["deliveryError"] = outcome.delivery_error
    human = [
        f"{action} {node_id} by {actor} (event {outcome.event['eventId']})",
        *(f"  -> {item.recipient}: {item.text}" for item in outcome.planned),
    ]
    if outcome.undelivered:
        human.append(
            f"  ({len(outcome.undelivered)} notification(s) undelivered; "
            "`hyprial pac notify resend` retries them)"
        )
    _emit(document, json_out, "\n".join(human))


@flag_app.command("set")
def flag_set(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node id owned by the acting owner."),
    actor: str = typer.Option(..., "--actor", help="Acting owner short name; must own the node."),
    reason_ref: str | None = typer.Option(None, "--reason-ref", help="Reference recording why the flag was set."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Set a flag (owner-only); the reactor notifies whoever turns next."""

    _flag_command(graph_id, node_id, actor, reason_ref, json_out, action="set")


@flag_app.command("reset")
def flag_reset(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node id owned by the acting owner."),
    actor: str = typer.Option(..., "--actor", help="Acting owner short name; must own the node."),
    reason_ref: str | None = typer.Option(None, "--reason-ref", help="Reference recording why the flag was reset."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Reset a flag (owner-only); already-turned downstream gets 已撤回."""

    _flag_command(graph_id, node_id, actor, reason_ref, json_out, action="reset")


@pac_app.command("events")
def pac_events(
    graph_id: str = typer.Argument(..., help="Graph to subscribe to."),
    after: int | None = typer.Option(None, "--after", help="Strict lower bound on journal seq (default 0)."),
    snapshot: bool = typer.Option(False, "--snapshot", help="Bootstrap one consistent snapshot@cursor."),
    follow: bool = typer.Option(False, "--follow", help="Wait for events; SIGINT/SIGTERM or output-pipe close exits."),
    journal_id: str | None = typer.Option(None, "--journal-id", help="Expected journal identity from the snapshot."),
    json_out: bool = typer.Option(False, "--json", help="JSON lines; errors go only to stderr."),
) -> None:
    """Public snapshot + typed journal outlet. Does not open a writable store."""
    from .stream import follow_lifetime, silence_broken_pipe

    projection = Projection(_database_path())
    try:
        require_initialized_hyprial_home()
        if snapshot and after is not None:
            raise PacError("PAC_EVENTS_ARGUMENT", "--snapshot and --after are mutually exclusive")
        with follow_lifetime(follow) as output:
            cursor = after if after is not None else 0
            if snapshot:
                document = projection.public_snapshot(graph_id)
                typer.echo(json.dumps(document, ensure_ascii=False))
                if not follow:
                    return
                cursor, journal_id = document["cursor"], document["journalId"]
            upper = None
            while True:
                if output is not None and output.closed():
                    return
                from .subscription import events_since
                page = events_since(_database_path(), graph_id, cursor,
                                    journal_id=journal_id, until=upper)
                journal_id = page["journalId"]
                upper = page["highWatermark"]
                for event in page["events"]:
                    typer.echo(json.dumps(event, ensure_ascii=False) if json_out else
                               f"#{event['seq']} {event['type']} {event['graphId']}")
                cursor = page["cursor"]
                if cursor < upper:
                    continue
                if not follow:
                    return
                upper = None
                sleep(0.2)  # no read transaction is held while waiting
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        silence_broken_pipe()
        return
    except (PacError, HYPRIALHomeNotInitialized) as error:
        document = {"ok": False, "code": error.code, "error": str(error)}
        if isinstance(error, PacError) and error.data:
            document["data"] = error.data
        typer.echo(json.dumps(document, ensure_ascii=False), err=True)
        raise typer.Exit(1) from None


# --------------------------------------------------------------------------- #
# notifications / clocks
# --------------------------------------------------------------------------- #


@notify_app.command("resend")
def notify_resend(
    graph_id: str = typer.Argument(..., help="Graph id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Retry exactly the notification rows that have no message_id yet."""

    _guard(json_out)
    reactor = _reactor(DaemonNotificationSender(_state_dir()))
    try:
        result = reactor.resend_undelivered(graph_id)
    except PacError as error:
        _fail(error, json_out)
    finally:
        reactor.close()
    _emit(
        {"ok": True, **result},
        json_out,
        f"resend: {result['delivered']} delivered, {result['undelivered']} still undelivered",
    )


@debug_app.command("clock")
def clock_tick(
    graph_id: str = typer.Argument(..., help="Graph id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Tick the clock nodes: derive overdue notifications (idempotent)."""

    _guard(json_out)
    reactor = _reactor(DaemonNotificationSender(_state_dir()))
    try:
        planned = reactor.tick_clocks(graph_id)
    except PacError as error:
        _fail(error, json_out)
    finally:
        reactor.close()
    _emit(
        {"ok": True, "overdue": [planned_to_json(item) for item in planned]},
        json_out,
        f"{len(planned)} overdue notification(s)",
    )


# --------------------------------------------------------------------------- #
# projection (spec ⑤ + headline acceptance #1)
# --------------------------------------------------------------------------- #


@pac_app.command("context")
def pac_context(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node whose work context to read."),
    json_out: bool = typer.Option(False, "--json", help="Stable node-context JSON contract."),
) -> None:
    """Read a node's brief/ref inputs and current activation at one cursor."""

    _guard(json_out)
    try:
        document = node_context(_database_path(), graph_id, node_id)
    except PacError as error:
        _fail(error, json_out)
    activation = document["currentActivation"]
    human = [
        f"graph {graph_id} node {node_id} version {document['version']} cursor {document['cursor']}",
        f"brief: {document['briefRef']}",
        f"completedCount: {document['completedCount']}",
        f"currentActivation: {activation['round'] if activation is not None else 'none'}",
        *(f"  predecessor {pred['nodeId']}: flag={pred['flag']} setBy={pred['setBy']} "
          f"setAt={pred['setAt']} reasonRef={pred['reasonRef']}" for pred in document["predecessors"]),
    ]
    if "deadlineMs" in document:
        human.append(f"deadlineMs: {document['deadlineMs']} (this clock only)")
    _emit(document, json_out, "\n".join(human))


@debug_app.command("restate")
def pac_restate(
    graph_id: str = typer.Argument(..., help="Graph id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Restate the whole flow from flag_events alone; cross-check notifications."""

    _guard(json_out)
    try:
        document = audit(_database_path(), graph_id)
    except KeyError:
        _fail(PacError(PAC_GRAPH_NOT_FOUND, f"graph {graph_id!r} not found"), json_out)
    except PacError as error:
        _fail(error, json_out)
    drift = document["drift"]
    human = [
        f"restating {graph_id} from {len(document['steps'])} flag events",
        *(
            f"  #{step['seq']} {step['action']} {step['node']} by {step['actor']}"
            + (
                " => "
                + "; ".join(
                    f"notify {item['recipient']} {item['text']}" for item in step["caused"]
                )
                if step["caused"]
                else ""
            )
            for step in document["steps"]
        ),
        f"drift: {'none' if not drift else str(len(drift)) + ' row(s)'}",
    ]
    _emit(document, json_out, "\n".join(human))
    if drift:
        raise typer.Exit(1)
