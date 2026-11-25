#!/usr/bin/env python3
"""E2E-001: two-machine send, reply, ack, offline queueing, and self-heal.

Primary mode uses real Docker network namespaces on an internal bridge,
current-source Linux Tailcat sidecars and an owned private DERP/STUN relay.
Each daemon listens on its private Unix backend and dials managed sidecar
slots; traffic crosses containers through the real network. The launcher
re-applies that private backend, pinned binary and relay on every boot.
Normal org invite/join initializes membership before the scenario proves:

  phase 1  discovery + bidirectional send/reply/ack across containers;
           the request goes to the advertised `targetUri` verbatim (the
           `agent:` display form), proving the copy-paste path delivers
  phase 2  offline queueing: stop B, A sends, start B, message is delivered
           from A's durable outbox and receipted (cross-machine inbox semantics)
  phase 3  restart self-heal: stop/start A, the mesh re-establishes, a
           message queued by B inside the restart window is delivered, and a
           second roundtrip succeeds
  phase 4  suspend/resume: `docker pause` freezes B with its network
           namespace and sockets retained (the laptop-lid-close shape stop/start cannot
           reproduce); A's message waits out the suspension and is then
           delivered exactly once
  phase 5  mesh partition heal: `docker network disconnect` drops the link
           while both containers keep running (real network flapping, not a
           process stop); reconnecting rebuilds presence both ways, flushes
           the backlog exactly once, and a fresh roundtrip succeeds
  phase 6  delivery terminal state across containers: every message ends
           `fetched` or `expired` in a durable record the sender pulls after
           the fact.  A third container joins with a short hold TTL so an
           `expired` verdict is produced in seconds, and the same verdict is
           then pulled from a container that never held the message -- which
           is the only assertion here that a broken cross-host `msg/status`
           leg cannot pass (the sender mirrors `fetched` locally the moment a
           receipt confirms, so a sender asking about its OWN delivered
           message is answered from its own ledger).  Every verdict is
           asserted together with the holder set it was merged from
  phase 7  mailbox custody: a fourth container runs with `as_mailbox`, so an
           undeliverable message is TAKEN by the mailbox (ownership transfer,
           decision G1) instead of being kept by the sender.  The mailbox --
           neither sender nor recipient -- writes the terminal record, and
           the sender pulls it back across containers

Cleanup is GUARANTEED, not best-effort: every resource uses the fixed
`hyprial-e2e001-*` prefix; a pre-clean removes leftovers from previous abnormal
exits before anything is created; the whole run is wrapped in try/finally with
SIGINT/SIGTERM handlers; and the script ends with a `docker ps -a` /
`docker network ls` / `docker volume ls` self-check that fails loudly if
anything under the prefix (or carrying the label) survived.  The image itself
is reused only when its `hyprial.e2e001.inputs` label matches a hash of the
Dockerfile's build inputs (spec-e2e001 追加 1+4), and only the newest two
labeled images are retained.  On failure only the evidence files are kept
(`/tmp/hyprial-e2e001-failure-<ts>/`), never the containers.

`--local` runs the loopback dual-home fallback (no Docker); it covers the same
persistence contract but not the cross-network semantics, and it does NOT run
phases 6-7 -- those need extra daemons with their own hold TTL and mailbox
role, which the loopback fallback has no way to configure per process.

The attended `[REAL ENV]` run (two Tailnet hosts) uses the same contract and
endpoints from docs/guides/zenoh-two-machine.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    IsolatedDaemon,
    artifacts_root,
    free_tcp_endpoint,
    hyprial_executable,
)
from _image_inputs import describe as describe_inputs
from _image_inputs import dirty_inputs, inputs_hash
from _presence_trace import PresenceTrace

from _two_machine.ports import TwoMachinePorts as _TwoMachinePorts
from _two_machine import image as _image_ops
from _two_machine import phases as _delivery_phases
from _two_machine import mailbox as _mailbox_phases
from _two_machine import tailcat as _tailcat_ops
from _two_machine.transport import shell as _shell
from _two_machine import local as _local
from _two_machine.transport import diagnostics as _diagnostics

def _two_machine_ports() -> _TwoMachinePorts:
    return _TwoMachinePorts(
        ANSWERED_GHOST_ACTOR=ANSWERED_GHOST_ACTOR,
        DRIVER=DRIVER,
        DockerError=DockerError,
        GHOST_ACTOR=GHOST_ACTOR,
        HOLD_TTL_MS=HOLD_TTL_MS,
        IMAGE=IMAGE,
        INPUTS_LABEL=INPUTS_LABEL,
        RELAY=RELAY,
        _DERP=_DERP,
        _ORG_NAMES=_ORG_NAMES,
        _INVITES=_INVITES,
        MAILBOX_HOLD_TTL_MS=MAILBOX_HOLD_TTL_MS,
        Mapping=Mapping,
        NETWORK=NETWORK,
        NODES=NODES,
        NODE_C=NODE_C,
        NODE_D=NODE_D,
        PREFIX=PREFIX,
        PROXY_BUILD_ARGS=PROXY_BUILD_ARGS,
        ROOT=ROOT,
        _LOOPBACK_PROXY=_LOOPBACK_PROXY,
        _capture_docker_state=_capture_docker_state,
        _ci_capture_enabled=_ci_capture_enabled,
        _is_protected=_is_protected,
        advertised_target=advertised_target,
        assert_delivered_once=assert_delivered_once,
        assert_endpoints=assert_endpoints,
        assert_managed_endpoints=assert_managed_endpoints,
        assert_holds=assert_holds,
        assert_no_known_loss=assert_no_known_loss,
        assert_terminal_record=assert_terminal_record,
        build_image=build_image,
        container_environment=container_environment,
        counts=counts,
        create_org_space=create_org_space,
        delivery_status=delivery_status,
        delivery_terminal_state=delivery_terminal_state,
        describe_inputs=describe_inputs,
        diagnostics_of=diagnostics_of,
        dirty_inputs=dirty_inputs,
        docker=docker,
        docker_available=docker_available,
        driver_uri=driver_uri,
        exec_json=exec_json,
        format_diagnostics=format_diagnostics,
        image_inputs_label=image_inputs_label,
        inputs_hash=inputs_hash,
        invite_org_owner=invite_org_owner,
        join_org_space=join_org_space,
        labeled_image_entries=labeled_image_entries,
        mailbox_custody=mailbox_custody,
        mesh_holders=mesh_holders,
        mesh_reconnect=mesh_reconnect,
        mesh_responders=mesh_responders,
        offline_queue=offline_queue,
        os=os,
        outbox_count=outbox_count,
        owner_of_node=owner_of_node,
        peer_of=peer_of,
        pending_ids=pending_ids,
        pre_clean=pre_clean,
        prefixed_containers=prefixed_containers,
        prefixed_networks=prefixed_networks,
        prefixed_volumes=prefixed_volumes,
        proxy_build_args=proxy_build_args,
        queue_while_unreachable=queue_while_unreachable,
        register_driver=register_driver,
        remove_resources=remove_resources,
        roundtrip=roundtrip,
        run_container=run_container,
        start_relay=start_relay,
        send_one=send_one,
        sh=sh,
        shutil=shutil,
        suspend_resume=suspend_resume,
        sweep_old_labeled_images=sweep_old_labeled_images,
        sys=sys,
        time=time,
        wait_daemon_ready=wait_daemon_ready,
        wait_for=wait_for,
        wait_for_mailbox=wait_for_mailbox,
        wait_for_target=wait_for_target,
        wait_for_terminal_state=wait_for_terminal_state,
        ALL_NODES=ALL_NODES,
        Any=Any,
        IsolatedDaemon=IsolatedDaemon,
        PRESENCE_TRACE=PRESENCE_TRACE,
        Path=Path,
        RunnerInterrupted=RunnerInterrupted,
        UTC=UTC,
        _URL_USERINFO=_URL_USERINFO,
        _URL_USERINFO_BYTES=_URL_USERINFO_BYTES,
        artifacts_root=artifacts_root,
        datetime=datetime,
        driver_uri_local=driver_uri_local,
        free_tcp_endpoint=free_tcp_endpoint,
        hyprial_executable=hyprial_executable,
        json=json,
        pending_ids_local=pending_ids_local,
        redact_argv=redact_argv,
        redact_text=redact_text,
        roundtrip_local=roundtrip_local,
        signal=signal,
        subprocess=subprocess,
        tempfile=tempfile,
        threading=threading,
        wait_for_target_local=wait_for_target_local,
        zenoh_of=zenoh_of,
        _OWNERS=_OWNERS,
        _DRIVERS=_DRIVERS,
    )

ROOT = Path(__file__).resolve().parents[2]
# Fixed resource prefix: containers, network, image label.  The image keeps a
# fixed tag (hyprial-e2e001:local) so rebuilds overwrite instead of piling up
# dangling images; dangling labeled layers are pruned after each build.
PREFIX = "hyprial-e2e001"
IMAGE = f"{PREFIX}:local"
NETWORK = f"{PREFIX}-net"
RELAY = f"{PREFIX}-relay"
_DERP: dict[str, object] = {}
_ORG_NAMES: dict[str, str] = {}
_INVITES: dict[tuple[str, str], str] = {}
NODES = (f"{PREFIX}-a", f"{PREFIX}-b")
# Phase 6 only.  Kept out of NODES so the two-machine indexing above stays
# exactly what it was; cleanup finds it through the shared name prefix.
NODE_C = f"{PREFIX}-c"
# Phase 7 only: the mailbox.  A node that takes custody changes what happens
# to EVERY message whose recipient is offline, so it must not exist while the
# earlier phases are exercising the sender-holds-it path.
NODE_D = f"{PREFIX}-d"
# Every container this runner may create.  Evidence capture and the cleanup
# self-check both walk this, so a new node has to be added here or it becomes
# invisible to both.
ALL_NODES = (*NODES, NODE_C, NODE_D, RELAY)
PRESENCE_TRACE = PresenceTrace(capacity=512)
_TRACE_CAPTURE_FAILURES = 0


def _ci_capture_enabled() -> bool:
    return bool(os.environ.get("HYPRIAL_E2E_ARTIFACTS") or os.environ.get("HYPRIAL_E2E_ROOT"))
# Reuse identity for the fixed tag (spec-e2e001 追加 1+4): the label carries
# a hash of the Dockerfile's actual build inputs, and an image is reused only
# when that hash matches the tested tree.  "Image exists" alone proves
# nothing about which source it was built from.
INPUTS_LABEL = "hyprial.e2e001.inputs"
# An actor nobody ever registers, addressed to a node that DOES exist.  A
# message to it is accepted and queued (`target-not-visible`) and can only
# end by the holder's TTL -- but the recipient's node is alive and answers
# every status query (with an empty, self-named frame), so under the #519
# projection its eviction stays a CERTAIN `expired`: an answered recipient
# with no better record cannot unconfirm the holder's own observation.
# node_b rather than node_a on purpose: with the ghost on b, BOTH 6d query
# legs (C local, A cross-boundary) rest on "recipient holder answered";
# on a the A leg would take the querying-node short-circuit instead, which
# is a different (#519) cell and would stay green even if a never answered.
ANSWERED_GHOST_ACTOR = f"agent:e2e:{NODES[1]}:ghost"
# An actor nobody ever registers, addressed to a node that never exists.  A
# message addressed to it is accepted and queued (`target-not-visible`),
# never delivered, and therefore reaches its terminal state the only way
# left: the holder's TTL. No node will ever answer for `e2e001-ghost-node`;
# phase 6e proves that this response-health fact does not rewrite the verdict.
# A well-formed URI for a node that never exists: undeliverable by
# construction (bare targets are rejected at the send boundary now).
GHOST_ACTOR = "agent:e2e:e2e001-ghost-node:ghost"
# Phase 6's hold TTL, applied to the third container alone.  A and B keep the
# product default because phases 2-5 park messages in an outbox across
# container stops that take tens of seconds.
HOLD_TTL_MS = 25_000
# Phase 7's mailbox TTL, deliberately longer than phase 6's.  `custody_ttl_ms`
# and the outbox TTL are the same knob, so this one number also bounds how
# long the mailbox may hold the message it is supposed to DELIVER -- and that
# path has to survive a full container restart of the recipient.  25s does
# not; the mailbox would evict the message while the recipient was booting.
MAILBOX_HOLD_TTL_MS = 60_000


class DockerError(RuntimeError):
    pass


class RunnerInterrupted(RuntimeError):
    pass


# Greedy up to the LAST "@" before any "/" or whitespace, so an unencoded "@"
# inside a password is still masked in full.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s]+@")
_URL_USERINFO_BYTES = re.compile(_URL_USERINFO.pattern.encode())


def redact_argv(args: list[str]) -> list[str]:
    "Mask URL userinfo (``user:pass@``) in every argument, by value.\n\n    The E2E-001 build passes the job's proxy as ``--build-arg HTTPS_PROXY=…``;\n    a proxy URL may carry credentials, and the argv lands in DockerError /\n    TimeoutExpired text, i.e. the CI log.  Redacting by value (any URL with\n    userinfo) rather than by key also covers args this function never heard of.\n    "
    return _shell.redact_argv(_two_machine_ports(), args)



def redact_text(text: str | bytes | None) -> str | bytes | None:
    "Same masking for free text: pip/uv/curl echo proxy URLs into their\n    output, so a failed build's stdout/stderr tail can carry them too."
    return _shell.redact_text(_two_machine_ports(), text)



def sh(args: list[str], *, timeout: float=60.0, check: bool=True) -> subprocess.CompletedProcess[str]:
    return _shell.sh(_two_machine_ports(), args, timeout=timeout, check=check)



def docker(args: list[str], *, timeout: float=60.0) -> str:
    return _shell.docker(_two_machine_ports(), args, timeout=timeout)



# R7 deleted the top-level `ps` CLI command.  When a two-machine step needs the
# full status snapshot (zenoh, pendingMessages, outboxCount, ...), read it over
# the container's own IPC socket instead of adding a CLI-only command back.
_PS_IPC_SCRIPT = """\
import json, os, socket, uuid
socket_path = os.path.join(os.environ["HARNESS_STATE_DIR"], "daemon.sock")
frame = {"version": 1, "id": uuid.uuid4().hex, "method": "ps", "params": {}}
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect(socket_path)
client.sendall(json.dumps(frame).encode() + b"\\n")
buffer = b""
while b"\\n" not in buffer:
    buffer += client.recv(65536)
print(json.dumps({"ok": True, **json.loads(buffer.split(b"\\n")[0])["result"]}))
"""


def exec_json(
    node: str, *cli_args: str, timeout: float = 30.0, check: bool = True
) -> dict[str, object]:
    """Run one hyprial CLI command inside a container and parse its JSON output."""

    if cli_args == ("ps",):
        completed = sh(
            ["docker", "exec", node, "python3", "-c", _PS_IPC_SCRIPT],
            timeout=timeout,
            check=check,
        )
    else:
        completed = sh(
            ["docker", "exec", node, "hyprial", *cli_args, "--json"],
            timeout=timeout,
            check=check,
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"{node} hyprial {' '.join(cli_args)} did not emit one JSON object: "
            f"{completed.stdout!r}"
        ) from error
    if not isinstance(value, dict):
        # Deliberate: contract violations are classified as RESULT FAIL by the
        # runner's AssertionError boundary.
        raise AssertionError(  # noqa: TRY004
            f"{node} hyprial {' '.join(cli_args)} returned a non-object"
        )
    if cli_args:
        try:
            PRESENCE_TRACE.record_cli(node, cli_args[0], value)
        except Exception as error:  # noqa: BLE001 - diagnostic failure cannot change CLI evidence
            global _TRACE_CAPTURE_FAILURES
            _TRACE_CAPTURE_FAILURES += 1
            if _TRACE_CAPTURE_FAILURES == 1:
                print(f"RESULT info E2E-001 presence observation lost ({type(error).__name__})")
    return value


def wait_for(probe, accept, *, timeout: float, interval: float=0.5, what: str='condition'):
    'Poll until `accept(probe())` is true or `timeout` expires.\n\n    Probe failures are transient by design (containers boot, docker exec can\n    fail for a second), so they are retried rather than raised mid-poll.  The\n    last failure is retained separately and re-raised on timeout: the accept\n    predicate only ever sees real probe values, and when a poll ultimately\n    fails, the ORIGINAL error (e.g. "container is not running") surfaces\n    instead of an unrelated AttributeError from inspecting an exception\n    object.\n    '
    return _shell.wait_for(_two_machine_ports(), probe, accept, timeout=timeout, interval=interval, what=what)



def pending_ids(value: dict[str, object]) -> list[object]:
    return _delivery_phases.pending_ids(_two_machine_ports(), value)



def outbox_count(value: dict[str, object]) -> int:
    return _delivery_phases.outbox_count(_two_machine_ports(), value)



def zenoh_of(value: dict[str, object]) -> dict[str, object]:
    return _delivery_phases.zenoh_of(_two_machine_ports(), value)



def assert_endpoints(label: str, value: dict[str, object], listen: str, connect: str) -> None:
    return _shell.assert_endpoints(_two_machine_ports(), label, value, listen, connect)


def assert_managed_endpoints(node: str, value: dict[str, object]) -> None:
    return _tailcat_ops.assert_endpoints(_two_machine_ports(), node, value)


def start_relay() -> None:
    return _tailcat_ops.start_relay(_two_machine_ports())



def wait_for_target(node: str, peer: str, *, absent: bool=False, timeout: float=45.0) -> None:
    return _delivery_phases.wait_for_target(_two_machine_ports(), node, peer, absent=absent, timeout=timeout)



def advertised_target(node: str, peer_actor: str) -> str:
    'The exact `targetUri` a peer advertises for `peer_actor` on `node`.'
    return _delivery_phases.advertised_target(_two_machine_ports(), node, peer_actor)



# resolve-or-reject (Allen b): probe identities are registered personas.
# Each container daemon registers the same short driver name; the wire
# identity is the canonical URI minted from the container's owner+node.
DRIVER = "e2e001-driver"

_OWNERS: dict[str, str] = {}
_DRIVERS: dict[str, str] = {}


def driver_uri(node: str) -> str:
    return _delivery_phases.driver_uri(_two_machine_ports(), node)



def driver_uri_local(daemon) -> str:
    return _local.driver_uri_local(_two_machine_ports(), daemon)



def register_driver(node: str) -> None:
    return _delivery_phases.register_driver(_two_machine_ports(), node)



def owner_of_node(node: str) -> str:
    'One container\'s owner, read back from its own `ps` report.'
    return _delivery_phases.owner_of_node(_two_machine_ports(), node)



def create_org_space(node: str, *, prefix: str, actor: str) -> str:
    'Create one group- org space; hard-cut agent-to-agent sends need it.'
    return _delivery_phases.create_org_space(_two_machine_ports(), node, prefix=prefix, actor=actor)



def invite_org_owner(node: str, space_id: str, owner: str) -> None:
    'Invite one owner into a space without a user-facing notification.'
    return _delivery_phases.invite_org_owner(_two_machine_ports(), node, space_id, owner)



def join_org_space(node: str, space_id: str, *, actor: str) -> None:
    'Join a space another container created and wait for it to compose.'
    return _delivery_phases.join_org_space(_two_machine_ports(), node, space_id, actor=actor)



def wait_daemon_ready(node: str) -> None:
    return _delivery_phases.wait_daemon_ready(_two_machine_ports(), node)



def roundtrip(phase: str, sender: str, sender_actor: str, receiver: str, receiver_actor: str, text: str, *, receiver_target: str | None=None) -> None:
    return _delivery_phases.roundtrip(_two_machine_ports(), phase, sender, sender_actor, receiver, receiver_actor, text, receiver_target=receiver_target)



# --------------------------------------------------------------------------
# Guaranteed cleanup: fixed prefix, pre-clean, self-check.
# --------------------------------------------------------------------------


def docker_available() -> None:
    return _image_ops.docker_available(_two_machine_ports(), )



def prefixed_containers() -> list[str]:
    return _image_ops.prefixed_containers(_two_machine_ports(), )



def prefixed_networks() -> list[str]:
    return _image_ops.prefixed_networks(_two_machine_ports(), )



def prefixed_volumes() -> list[str]:
    return _image_ops.prefixed_volumes(_two_machine_ports(), )



def remove_resources() -> list[str]:
    'Remove every `{PREFIX}` container, network and labeled volume.\n\n    Each step swallows errors so a failure here is surfaced by the self-check\n    instead of silently stopping the rest of the cleanup.\n    '
    return _image_ops.remove_resources(_two_machine_ports(), )



def self_check() -> list[str]:
    'Assert no `{PREFIX}` container/network and no labeled volume survived.'
    return _image_ops.self_check(_two_machine_ports(), )



def pre_clean() -> None:
    'Remove leftovers from a previous abnormal exit before creating anything.'
    return _image_ops.pre_clean(_two_machine_ports(), )



def image_inputs_label() -> str | None:
    'The inputs label on the current fixed-tag image, None when absent.\n\n    Raises DockerError when the image itself does not exist — callers treat\n    that as "must build".\n    '
    return _image_ops.image_inputs_label(_two_machine_ports(), )



def labeled_image_entries() -> list[tuple[str, str]]:
    "``(createdAt, fullId)`` for every image carrying the inputs label,\n    dangling ones included (a dangling image is just an overwritten tag,\n    and it keeps its labels).\n\n    Selection is keyed on ``hyprial.e2e001.inputs`` — the label only this\n    runner's builds carry — never on the broader ``hyprial-e2e001=1``\n    (批复 §1b): an image missing the inputs label must never be a sweep\n    candidate, no matter what other labels it has.\n    "
    return _image_ops.labeled_image_entries(_two_machine_ports(), )



def sweep_old_labeled_images(*, keep: int=2, protect: str | None=None) -> list[str]:
    'Keep the newest ``keep`` labeled images plus the protected (current)\n    one; remove older labeled images (spec-e2e001 追加 1.3).\n\n    Only images carrying this runner\'s label are ever candidates — no global\n    prune, no builder cache, nothing of other tenants on the shared VM.\n    Replaces the previous ``image prune --filter label=…``, which deleted\n    every dangling labeled image and thereby made "keep the last two"\n    impossible.\n    '
    return _image_ops.sweep_old_labeled_images(_two_machine_ports(), keep=keep, protect=protect)



def _is_protected(entry: tuple[str, str], protect: str | None) -> bool:
    return _image_ops._is_protected(_two_machine_ports(), entry, protect)



PROXY_BUILD_ARGS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
_LOOPBACK_PROXY = re.compile(r"^(?P<scheme>[a-z]+://)(?:127\.0\.0\.1|localhost|\[::1\])(?P<rest>[:/].*)?$")


def proxy_build_args(environ: Mapping[str, str], platform: str=sys.platform) -> list[str]:
    "``--build-arg`` pairs that route the build's pip/uv through the host proxy.\n\n    The image build's ``RUN pip install uv && uv sync`` otherwise goes straight\n    to PyPI, and on the macOS host runner that path times out (measured\n    2026-09-27: direct ReadTimeout after 86 s vs 12 s through the proxy; task\n    35833's E2E-001 failed this way).  The proxy names are Docker's predefined\n    ARGs: they are neither part of the layer-cache key nor persisted in the\n    image, so passing them changes neither reuse nor the inputs label.\n\n    A loopback proxy (e2e-macos-isolated.yml exports ``http://127.0.0.1:7896``) is the job\n    host's own loopback; inside the build container it has to be spelled\n    ``host.docker.internal``.  That name resolves by default only under Docker\n    Desktop / OrbStack on macOS (the one runner that runs E2E-001, macos-host).\n    Elsewhere a loopback proxy is unreachable from the build container either\n    way, so it is not passed at all: the build keeps its previous direct path\n    instead of trading it for a dead proxy.\n    "
    return _image_ops.proxy_build_args(_two_machine_ports(), environ, platform)



def build_image(*, force: bool) -> None:
    'Build (or reuse) the fixed-tag image, keyed on its build inputs.\n\n    Reuse requires ALL of: no --rebuild, no dirty build inputs (the label\n    describes the tested commit, and a dirty tree builds content the label\n    would not stand behind), and an existing image whose inputs label equals\n    the hash of the tree under test.  Anything else rebuilds.\n    '
    return _image_ops.build_image(_two_machine_ports(), force=force)



def container_environment(node: str, *, extra_env: dict[str, str] | None=None) -> dict[str, str]:
    'Every E2E-001 container\'s environment, built the way _daemon_runner\n    builds IsolatedDaemon\'s: the isolation defines the identity.\n\n    #361 made the node\'s owner mandatory (HYPRIAL_OWNER, else settings.json\n    "owner", else the daemon refuses to start) and U5 removed `init --owner`;\n    the container mounts a fresh HYPRIAL_HOME with no settings.json, so\n    without HYPRIAL_OWNER the daemon never comes up ("user identity is not\n    set" -- isolated-macos task 9379/9294, red on every nightly since).\n    The value is fixed, never inherited from the host shell: GHOST_ACTOR\n    already mints `agent:e2e:...`, and driver_uri reads the owner back from\n    the daemon\'s own report, so every wire identity stays on `e2e`.  A phase\n    needing a different owner passes it via extra_env, which merges last and\n    therefore wins (the same rule _daemon_runner documents).\n    '
    return _image_ops.container_environment(_two_machine_ports(), node, extra_env=extra_env)



def run_container(node: str, *, extra_env: dict[str, str] | None=None) -> None:
    return _image_ops.run_container(_two_machine_ports(), node, extra_env=extra_env)



def run_docker(*, rebuild: bool) -> None:
    return _image_ops.run_docker(_two_machine_ports(), rebuild=rebuild)



def peer_of(node: str) -> str:
    'The other two-machine node: endpoints are symmetric per pair.'
    return _delivery_phases.peer_of(_two_machine_ports(), node)



def offline_queue(sender: str, sender_actor: str, receiver: str, receiver_actor: str) -> str:
    'Stop the receiver, queue a message on the sender, then deliver on restart.\n\n    Returns the message id so phase 6 can ask, much later and after the\n    sender has itself been restarted, what finally happened to it.\n    '
    return _delivery_phases.offline_queue(_two_machine_ports(), sender, sender_actor, receiver, receiver_actor)



def queue_while_unreachable(phase: str, sender: str, sender_actor: str, receiver_actor: str, text: str) -> str:
    'Send while the receiver cannot answer; assert durable queueing.'
    return _delivery_phases.queue_while_unreachable(_two_machine_ports(), phase, sender, sender_actor, receiver_actor, text)



def assert_delivered_once(phase: str, sender: str, receiver: str, receiver_actor: str, message_id: str) -> None:
    'The recovery floor: not lost, not duplicated, outbox drains, ack works.'
    return _delivery_phases.assert_delivered_once(_two_machine_ports(), phase, sender, receiver, receiver_actor, message_id)



def suspend_resume(phase: str, sender: str, sender_actor: str, receiver: str, receiver_actor: str) -> None:
    '`docker pause` freezes the process but keeps TCP established: the\n    laptop-lid-close shape.  The peer looks connected yet never answers,\n    which stop/start cannot reproduce.'
    return _delivery_phases.suspend_resume(_two_machine_ports(), phase, sender, sender_actor, receiver, receiver_actor)



def mesh_reconnect(phase: str, sender: str, sender_actor: str, receiver: str, receiver_actor: str) -> None:
    '`docker network disconnect` drops the link while both containers keep\n    running: real network flapping, not a process stop.  The mesh must\n    rebuild on `connect` and durable messages must catch up.'
    return _delivery_phases.mesh_reconnect(_two_machine_ports(), phase, sender, sender_actor, receiver, receiver_actor)



# --------------------------------------------------------------------------
# Phase 6: durable delivery terminal state, pulled across containers.
# --------------------------------------------------------------------------


def delivery_status(
    node: str, sender_actor: str, message_id: str, *, timeout_seconds: float = 5.0
) -> dict[str, object]:
    """Ask `node` what became of one message `sender_actor` sent."""

    return _diagnostics.delivery_status(_two_machine_ports(), node, sender_actor, message_id, timeout_seconds=timeout_seconds)


def diagnostics_of(label: str, value: dict[str, object]) -> dict[str, object]:
    return _diagnostics.diagnostics_of(_two_machine_ports(), label, value)


def format_diagnostics(value: dict[str, object]) -> str:
    return _diagnostics.format_diagnostics(_two_machine_ports(), value)


def assert_no_known_loss(label: str, value: dict[str, object]) -> None:
    """No reply may be dropped on the way into a verdict.

    This does NOT assert the holder set was complete -- no pull can know that.
    It asserts the weaker, checkable thing: nothing that answered was thrown
    away, so the verdict rests on everything that did answer.
    """

    return _diagnostics.assert_no_known_loss(_two_machine_ports(), label, value)


def assert_terminal_record(label: str, value: dict[str, object], *, sender_actor: str, message_id: str, expected_state: str, expected_holders: tuple[str, ...], expected_reason: str | None=None) -> dict[str, object]:
    'Assert the querier really holds the verdict, not merely that it replied.\n\n    `state` on its own would be a weak assertion: `pending` and `unknown` are\n    perfectly successful, non-error answers, and they are exactly what a\n    broken pull returns.  So the record body has to be there too, and it has\n    to name the same message, the same sender, and a holder that could\n    plausibly have observed the outcome.\n    '
    return _delivery_phases.assert_terminal_record(_two_machine_ports(), label, value, sender_actor=sender_actor, message_id=message_id, expected_state=expected_state, expected_holders=expected_holders, expected_reason=expected_reason)



def merged_holder(value: dict[str, object]) -> object:
    records = value.get("records")
    if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict):
        return records[0].get("holder")
    return None


def mesh_holders(value: dict[str, object]) -> list[object]:
    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return []
    holders = diagnostics.get("meshHolders")
    return list(holders) if isinstance(holders, list) else []


def mesh_responders(value: dict[str, object]) -> list[object]:
    """The nodes that ANSWERED the status query, tree-robustly.

    #528 renamed the published meaning of `meshHolders` to record holders and
    moved the answering nodes to `meshResponders`.  Pre-#528 trees publish the
    responder set as `meshHolders`.  Read whichever exists so the phase-6
    assertions pin "who answered" on both trees.
    """

    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return []
    responders = diagnostics.get("meshResponders")
    if isinstance(responders, list):
        return list(responders)
    holders = diagnostics.get("meshHolders")
    return list(holders) if isinstance(holders, list) else []


def wait_for_terminal_state(
    node: str,
    sender_actor: str,
    message_id: str,
    expected_state: str,
    *,
    timeout: float,
    what: str,
    expected_holder: str | None = None,
    required_mesh_holders: tuple[str, ...] = (),
) -> dict[str, object]:
    """Poll until the verdict AND the set it came from are both settled.

    Polling on `state` alone would accept the first answer that happened to
    arrive, which is how a partial holder set passes for a complete one: the
    state is identical either way.  Waiting for the holders too turns a race
    into a retry instead of a flaky assertion.
    """

    def settled(value: dict[str, object]) -> bool:
        if value.get("state") != expected_state:
            return False
        if expected_holder is not None and merged_holder(value) != expected_holder:
            return False
        return set(required_mesh_holders) <= set(mesh_holders(value))

    return wait_for(
        lambda: delivery_status(node, sender_actor, message_id),
        settled,
        timeout=timeout,
        interval=1.0,
        what=what,
    )


def send_one(
    label: str, node: str, sender_actor: str, target: str, text: str
) -> str:
    outbound = exec_json(
        node, "message", "send", "--from", sender_actor, "--to", target,
        "--summary", text, text, timeout=60.0
    )
    message_id = outbound.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError(f"{label} send returned no messageId: {outbound}")
    return message_id


def delivery_terminal_state(offline_message_id: str) -> None:
    'Phase 6: `fetched` and `expired` are durable, and pullable off-node.\n\n    Read the phase names literally.  6a and 6b are the sender asking about its\n    own message, and the sender mirrors `fetched` into its own ledger the\n    moment a receipt confirms (`InboxService._attempt_direct`), so they prove\n    the record exists and survives -- they do NOT prove the cross-container\n    query leg.  6c and 6d do: the querying container provably holds no record\n    of its own, so any verdict it reports crossed the network.\n    '
    return _delivery_phases.delivery_terminal_state(_two_machine_ports(), offline_message_id)



# --------------------------------------------------------------------------
# Phase 7: mailbox custody -- ownership transfer, and a non-sender holder's
# terminal record pulled back by the sender.
# --------------------------------------------------------------------------


def counts(node: str) -> tuple[int, int]:
    '`(outbox, custody)` for one node: the two places a held message can be.'
    return _mailbox_phases.counts(_two_machine_ports(), node)



def assert_holds(label: str, node: str, *, outbox: int, custody: int, timeout: float=30.0) -> None:
    'Wait for the hold counts to settle, then pin them exactly.\n\n    Polling rather than sampling once: custody transfer is several steps\n    (commit outbox row, hand over, delete row) and a single sample can land\n    mid-transfer.  The assertion itself stays exact.\n    '
    return _mailbox_phases.assert_holds(_two_machine_ports(), label, node, outbox=outbox, custody=custody, timeout=timeout)



def wait_for_mailbox(node: str, mailbox: str, *, timeout: float=90.0) -> None:
    "Wait until `node` can actually see the mailbox's liveliness token.\n\n    Without this the send races the token: a sender that has not seen a\n    mailbox yet keeps the message in its own outbox, which is correct\n    behaviour and the wrong phase.\n    "
    return _mailbox_phases.wait_for_mailbox(_two_machine_ports(), node, mailbox, timeout=timeout)



def mailbox_custody(recipient_node: str) -> None:
    "Phase 7: a mailbox takes the message, and records its own verdict.\n\n    Design section 4 (decision G1): with a mailbox present the mailbox\n    **takes** an undeliverable message -- ownership transfer, not a copy --\n    and without one the sending daemon keeps it.  Phases 2-5 cover the second\n    half; nothing covered the first, because no daemon ever set `as_mailbox`.\n\n    Two messages, because one message cannot end both ways:\n\n      7a  ownership transfer itself: the recipient is offline, the mailbox\n          takes custody, and the sender is left holding NOTHING\n      7b  `fetched` through the mailbox: the recipient returns, the mailbox\n          delivers what it was holding, and the mailbox's own record appears\n          in the sender's holder set\n      7c  `expired` written by a holder that is neither sender nor recipient,\n          on the mailbox's own TTL, and pulled back by the sender across\n          containers with `holder` naming the mailbox.  This is the case\n          PR #93 could not reach without a mailbox node.\n\n    Order matters and is not the narrative order.  `custody_ttl_ms` is the\n    same knob as the hold TTL, so the mailbox is also counting down on the\n    message it is supposed to deliver: 7b has to complete (including the\n    recipient's whole container restart) before that clock runs out, while\n    7c's message simply waits out the TTL with nothing else pending.\n    "
    return _mailbox_phases.mailbox_custody(_two_machine_ports(), recipient_node)



def _capture_docker_state(label: str) -> None:
    'One bounded A/B/C state snapshot; diagnostics never decide the scene.'
    return _diagnostics._capture_docker_state(_two_machine_ports(), label)



def _flush_ci_presence_trace() -> None:
    'Persist only the sanitized ring on a CI PASS without copying logs.'
    return _diagnostics._flush_ci_presence_trace(_two_machine_ports(), )



def capture_failure(label: str='failure') -> Path:
    'Dump container logs and daemon evidence to a kept directory.\n\n    Uses `docker cp` because it also works on stopped containers (a failure\n    may occur mid offline-queue phase).  Containers are still removed by the\n    cleanup; only the evidence directory is retained.\n\n    `--keep-evidence` runs this on a PASS too.  The `msg/status` diagnostics\n    live half on the querying node and half on the answering one -- a node\n    holding no records replies with nothing at all, so only its own log can\n    say whether it was ever asked -- and that half is unreachable once the\n    containers are reaped.\n    '
    return _diagnostics.capture_failure(_two_machine_ports(), label)



# --------------------------------------------------------------------------
# Local fallback mode (loopback dual-home; no Docker required)
# --------------------------------------------------------------------------


def run_local() -> None:
    return _local.run_local(_two_machine_ports(), )



def wait_for_target_local(daemon: IsolatedDaemon, actor: str, *, absent: bool=False) -> None:
    return _local.wait_for_target_local(_two_machine_ports(), daemon, actor, absent=absent)



def pending_ids_local(value: dict[str, object]) -> list[object]:
    return _local.pending_ids_local(_two_machine_ports(), value)



def roundtrip_local(phase: str, sender: IsolatedDaemon, sender_actor: str, receiver: IsolatedDaemon, receiver_actor: str, text: str) -> None:
    return _local.roundtrip_local(_two_machine_ports(), phase, sender, sender_actor, receiver, receiver_actor, text)



# --------------------------------------------------------------------------
# Entry point: signal handling + guaranteed cleanup + self-check.
# --------------------------------------------------------------------------


def _install_signal_handlers() -> dict[int, Any]:
    'Convert SIGINT/SIGTERM into RunnerInterrupted so the finally runs.'
    return _shell._install_signal_handlers(_two_machine_ports(), )



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local",
        action="store_true",
        help="use the loopback dual-home fallback instead of Docker containers",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a Docker image rebuild even if hyprial-e2e001:local exists",
    )
    parser.add_argument(
        "--keep-evidence",
        action="store_true",
        help=(
            "dump container logs and daemon.jsonl even on PASS; the answering "
            "side of a msg/status query is only visible in its own log"
        ),
    )
    args = parser.parse_args()
    _install_signal_handlers()

    error: BaseException | None = None
    aborted = False
    logs_dir: Path | None = None
    cleanup_problems: list[str] = []
    try:
        if args.local:
            run_local()
        else:
            run_docker(rebuild=args.rebuild)
    except RunnerInterrupted as interrupt:
        aborted = True
        error = interrupt
    except (AssertionError, DockerError, OSError, KeyError, subprocess.TimeoutExpired) as caught:
        error = caught
    finally:
        # Guaranteed: evidence first, then resource removal, then self-check.
        # Every step is isolated so a second Ctrl-C or one failing step cannot
        # skip the rest; problems are surfaced instead of swallowed silently.
        if not args.local:
            if error is not None:
                _capture_docker_state("failure")
            if error is not None or args.keep_evidence:
                try:
                    logs_dir = capture_failure(
                        "failure" if error is not None else "evidence"
                    )
                except Exception as capture_error:  # noqa: BLE001
                    cleanup_problems.append(f"evidence capture: {capture_error}")
            elif _ci_capture_enabled():
                _flush_ci_presence_trace()
            try:
                cleanup_problems.extend(remove_resources())
            except Exception as cleanup_error:  # noqa: BLE001
                cleanup_problems.append(f"resource removal: {cleanup_error}")
            try:
                leftovers = self_check()
                if leftovers:
                    cleanup_problems.append(
                        f"self-check found leftover {PREFIX} resources: {leftovers}"
                    )
            except Exception as check_error:  # noqa: BLE001
                cleanup_problems.append(f"self-check: {check_error}")
        if cleanup_problems and error is None:
            error = RuntimeError("cleanup/self-check failed")

    if cleanup_problems:
        print(f"RESULT FAIL E2E-001 {error}; cleanup: {'; '.join(cleanup_problems)}")
        return 1
    if error is None:
        if logs_dir is not None:
            print(f"RESULT info evidence kept in {logs_dir}")
        if args.local:
            print(
                "RESULT PASS E2E-001 two-machine-roundtrip mode=local "
                "phases=2 endpoints=explicit restartSurvived=true roundtrips=2"
            )
        else:
            print(
                "RESULT PASS E2E-001 two-machine-roundtrip mode=docker "
                f"phases=7 containers=5 daemons=4 network={NETWORK} cleaned=true "
                "offlineQueueing=true restartSelfHeal=true "
                "restartWindowDelivery=true suspendResume=true "
                "meshPartitionHeal=true advertisedTargetUri=true "
                "fetchedPulledBySender=true fetchedPulledOffNode=true "
                "expiredOnHolderTtl=true expiredPulledOffNode=true "
                "answeredRecipientCertainExpired=true "
                "absentRecipientExpired=true "
                "holderSetComplete=true authoritativeRecordWins=true "
                "mailboxTookCustody=true senderReleasedOwnership=true "
                "mailboxRecordedExpired=true mailboxVerdictPulledBySender=true"
            )
        return 0
    if aborted:
        kept = f"; logs kept in {logs_dir}" if logs_dir is not None else ""
        print(f"RESULT ABORTED E2E-001 {error}{kept}")
        return 130
    kept = f"; logs kept in {logs_dir}" if logs_dir is not None else ""
    print(f"RESULT FAIL E2E-001 {error}{kept}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
