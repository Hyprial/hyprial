"""``hyprial doctor`` diagnostic checks."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import json
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
def _tailcat_sidecar_doctor_check() -> JsonObject:
    """Tailcat cutover check: the sidecar is locatable and passes version.

    Replaces the old tsnet sidecar check: the binary must be found
    (``HYPRIAL_TAILCAT_BINARY`` > ``$HYPRIAL_HOME/bin/hyprial-tailcat``) and
    its ``version`` output must pin the Tailcat engine commit this daemon
    was cut against.
    """
    services = get_services()

    from hyprial.daemon import (
        TailcatSidecarError,
        verify_tailcat_sidecar,
    )

    home = services._hyprial_home()
    try:
        record = verify_tailcat_sidecar(home)
    except TailcatSidecarError as error:
        # Missing means cross-machine networking is not set up yet (the local
        # daemon still works): warn. Present-but-invalid is a real fault: fail.
        return {
            "name": "tailcat-sidecar",
            "status": "warn" if error.code == "SIDECAR_MISSING" else "fail",
            "detail": f"{error.code}: {error}",
            "action": {
                "command": "hyprial login",
                "description": (
                    "Install the hyprial-tailcat sidecar (build "
                    "sidecar/hyprial-tailcat into $HYPRIAL_HOME/bin or set "
                    "HYPRIAL_TAILCAT_BINARY), then rerun login to provision "
                    "the device key."
                ),
            },
        }
    return {
        "name": "tailcat-sidecar",
        "status": "ok",
        "detail": (
            f"tailcat sidecar verified (sidecar {record.get('sidecar')}, "
            "protocol v3)"
        ),
    }


def _device_key_doctor_check() -> JsonObject:
    """Tailcat cutover check: the device key file and device record exist."""
    services = get_services()

    from hyprial.identity import (
        device_key_path,
        read_device_record,
    )

    home = services._hyprial_home()
    key_file = device_key_path(home)
    try:
        record = read_device_record(home)
    except ValueError as error:
        return {
            "name": "device-key",
            "status": "fail",
            "detail": f"the device record is unreadable: {error}",
            "action": {
                "command": "hyprial login",
                "description": (
                    "Re-provision the device key and record; the identity "
                    "is not affected."
                ),
            },
        }
    if record is None or not key_file.is_file():
        missing = []
        if not key_file.is_file():
            missing.append(f"key file {key_file}")
        if record is None:
            missing.append("the device record")
        return {
            # Not provisioned yet (no login on this home): warn, not fail.
            "name": "device-key",
            "status": "warn",
            "detail": "missing " + " and ".join(missing),
            "action": {
                "command": "hyprial login",
                "description": (
                    "Provision the device key (login retries the device "
                    "stage alone; the identity stays committed)."
                ),
            },
        }
    return {
        "name": "device-key",
        "status": "ok",
        "detail": (
            f"device {record.device_id} (owner {record.owner}, key "
            f"generation {record.key_generation})"
        ),
    }


def _doctor_result() -> JsonObject:
    services = get_services()
    checks: list[JsonObject] = []
    # Local tailnet-cutover checks run without a daemon: the sidecar binary
    # and the on-disk device key/record are host facts, not IPC facts.
    checks.append(_tailcat_sidecar_doctor_check())
    checks.append(_device_key_doctor_check())
    try:
        result = services._daemon_request(
            "ps",
            {},
            timeout=7.0,
            restore_wait=0.0,
        )
        running = (
            isinstance(result, dict)
            and isinstance(result.get("daemon"), dict)
            and result["daemon"].get("running") is True
        )
        if running:
            checks.append(
                {"name": "daemon", "status": "ok", "detail": "daemon IPC is available"}
            )
            checks.append(_zenoh_doctor_check(result))
            duplicate_check = _duplicate_instance_doctor_check(result)
            if duplicate_check is not None:
                checks.append(duplicate_check)
            maintenance_check = _maintenance_doctor_check(result)
            if maintenance_check is not None:
                checks.append(maintenance_check)
            dsh_check = _dsh_doctor_check(result)
            if dsh_check is not None:
                checks.append(dsh_check)
            lark_check = _lark_inbound_doctor_check(result)
            if lark_check is not None:
                checks.append(lark_check)
            channel_check = _mcp_channel_doctor_check(result)
            if channel_check is not None:
                checks.append(channel_check)
            historical_inbox_check = _historical_inbox_doctor_check(result)
            if historical_inbox_check is not None:
                checks.append(historical_inbox_check)
            restore_check = _restore_doctor_check(result)
            if restore_check is not None:
                checks.append(restore_check)
            cleanup_check = _workflow_worker_cleanup_doctor_check(result)
            if cleanup_check is not None:
                checks.append(cleanup_check)
            pac_gc_check = _pac_gc_doctor_check(result)
            if pac_gc_check is not None:
                checks.append(pac_gc_check)
            routine_check = _routine_health_doctor_check()
            if routine_check is not None:
                checks.append(routine_check)
        else:
            checks.append(
                {
                    "name": "daemon",
                    "status": "fail",
                    "detail": "daemon did not report a running process",
                    "action": {
                        "command": "hyprial init",
                        "description": "Start the Harness daemon.",
                    },
                }
            )
    except (services.CliError, ipc_errors.TransientDaemonError) as error:
        checks.append(
            {
                "name": "daemon",
                "status": "fail",
                "detail": str(error),
                "action": {
                    "command": "hyprial init",
                    "description": "Start the Harness daemon.",
                },
            }
        )
    summary = {
        status: sum(item["status"] == status for item in checks)
        for status in ("ok", "warn", "fail")
    }
    return {
        "ok": summary["fail"] == 0,
        "schemaVersion": 1,
        "checks": checks,
        "summary": summary,
    }


def _restore_doctor_check(result: JsonObject) -> JsonObject | None:
    restore = result.get("restore")
    if not isinstance(restore, dict):
        return None
    suppressed = restore.get("suppressedCount", 0)
    unknown = restore.get("unknownActivityCount", 0)
    blocked = restore.get("blockedCount", 0)
    oldest = restore.get("oldestIdleAgeMs")
    if (
        not isinstance(suppressed, int)
        or not isinstance(unknown, int)
        or not isinstance(blocked, int)
    ):
        return None
    detail = (
        f"dormant={suppressed}, blocked={blocked}, activityUnknown={unknown}, "
        f"oldestIdleMs={oldest if isinstance(oldest, int) else 'none'}"
    )
    if restore.get("degraded") is True:
        return {
            "name": "restore-policy-degraded",
            "status": "warn",
            "detail": f"{detail}; {restore.get('degradedReason', 'policy unreadable')}",
            "action": {
                "command": "hyprial agent keep list",
                "description": (
                    "Repair the restore policy or keep-list; degraded startup "
                    "restores connectors to preserve availability."
                ),
            },
        }
    if suppressed or blocked:
        return {
            "name": "agent-restore",
            "status": "warn",
            "detail": detail,
            "action": {
                "command": "hyprial agent keep add <name>",
                "description": (
                    "Keep or explicitly start dormant agents; clear blocked "
                    "agents with hyprial agent unblock <name>."
                ),
            },
        }
    return {"name": "agent-restore", "status": "ok", "detail": detail}


def _routine_health_doctor_check() -> JsonObject | None:
    """Surface quarantined routines and address migrations (2026-09-14).

    A routine whose stored spec no longer validates (e.g. a legacy bare-name
    ``escalate_to`` with no unique roster match) is quarantined: it stops
    scheduling and refuses resume until the spec is fixed. That must be
    visible from ``hyprial doctor`` -- the incident this fixes was fifteen
    days of silence. The address-migration ledger is reported as metrics so
    automatic rewrites are auditable without paging anyone.
    """
    services = get_services()

    try:
        audit = services._daemon_request("routine.audit", {}, timeout=2.0)
    except (services.CliError, ipc_errors.TransientDaemonError):
        return None
    if not isinstance(audit, dict):
        return None
    quarantined = [
        item
        for item in audit.get("quarantined", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    migrations = audit.get("addressMigrations", [])
    migration_list = (
        [
            {
                "routine": m["routine"],
                "field": m["field"],
                "before": m["before"],
                "after": m["after"],
            }
            for m in migrations
            if isinstance(m, dict)
        ]
        if isinstance(migrations, list)
        else []
    )
    if not quarantined and not migration_list:
        return None
    check: JsonObject = {
        "name": "routine-addresses",
        "status": "warn" if quarantined else "ok",
        "detail": (
            f"{len(quarantined)} routine(s) quarantined for schema faults "
            f"(scheduling stopped, resume refused); {len(migration_list)} stored "
            "address migration(s) applied automatically, resolved against THIS "
            "machine's agents roster (confirm each rewrite is the intended "
            "recipient — a same-name agent on another machine would have been "
            "redirected silently)"
        ),
        "metrics": {
            "quarantined": [item["name"] for item in quarantined],
            "addressMigrations": migration_list,
        },
    }
    if quarantined:
        check["action"] = {
            "command": "hyprial routine status <name> --json",
            "description": (
                "Fix the quarantined spec (full agent URI / route: / user: "
                "addresses only), then remove and re-add the routine."
            ),
        }
    return check


def _zenoh_doctor_check(result: JsonObject) -> JsonObject:
    """Check the running daemon's explicit Zenoh transport endpoints.

    Discovery is disabled by design, so a daemon with neither a listen nor a
    connect endpoint is deterministically isolated from every other node.
    That is a hard failure (not a warning): the node cannot send or receive
    across the mesh no matter what.
    """

    zenoh = result.get("zenoh")
    if not isinstance(zenoh, dict):
        return {
            "name": "zenoh",
            "status": "warn",
            "detail": "daemon did not report Zenoh endpoints; upgrade the daemon",
        }
    listen = zenoh.get("listen")
    connect = zenoh.get("connect")
    if not isinstance(listen, list) or not isinstance(connect, list):
        listen = connect = ()
    if not listen and not connect:
        return {
            "name": "zenoh",
            "status": "fail",
            "detail": (
                "no explicit Zenoh listen/connect endpoints are configured and "
                "auto-discovery is disabled; this node cannot reach any other "
                "node (configuration check: no endpoints means no peers by "
                "construction)"
            ),
            "action": {
                "command": (
                    "hyprial init --listen tcp/<this-host>:<port> "
                    "--connect tcp/<peer-host>:<port>"
                ),
                "description": "Configure explicit Zenoh endpoints, then restart the daemon.",
            },
        }
    return {
        "name": "zenoh",
        "status": "ok",
        "detail": (
            "explicit Zenoh endpoints are configured; configured is not "
            "reachable -- actual peer connectivity must be proven by a real "
            "send/ack"
        ),
        "metrics": {"listen": list(listen), "connect": list(connect)},
    }


def _maintenance_doctor_check(result: JsonObject) -> JsonObject | None:
    """Fail when the daemon's own maintenance tick has stalled.

    The tick drives delivery retries, worker reconcile and every timer; on
    2026-09-27 it stopped for 40 minutes while ``ps`` kept answering, so a
    daemon that answers IPC is not evidence that it is doing its work.
    Returns None against a daemon that does not report the field.
    """

    info = result.get("maintenance")
    if not isinstance(info, dict):
        return None
    if info.get("stalled") is not True:
        return {
            "name": "maintenance-tick",
            "status": "ok",
            "detail": "the daemon's maintenance tick is completing on schedule.",
        }
    return {
        "name": "maintenance-tick",
        "status": "fail",
        "detail": (
            "the daemon's maintenance tick has not completed for "
            f"{info.get('stalledSeconds')}s; it is in phase "
            f"{info.get('phase')!r}. Deliveries, worker turns and timers wait on "
            "it. daemon.maintenance.stalled in the daemon log names the stack "
            "and the transport lock holder."
        ),
    }


def _duplicate_instance_doctor_check(result: JsonObject) -> JsonObject | None:
    """Surface the daemon's duplicate-instance verdict.

    Returns None against a daemon old enough to not report the field -- the
    check's absence is then the honest signal, exactly like the other
    version-gated checks.  The detail repeats the coverage limit from the
    daemon's payload: mesh detection sees only peers that declare a
    generation liveliness token, so a green check must never be read as
    "no duplicate exists anywhere".
    """

    info = result.get("duplicateInstance")
    if not isinstance(info, dict):
        return None
    coverage = info.get("meshDetectionCoverage")
    coverage_note = f" Coverage: {coverage}" if isinstance(coverage, str) else ""
    if info.get("active") is not True:
        return {
            "name": "duplicate-instance",
            "status": "ok",
            "detail": (
                "no duplicate daemon instance of this node identity detected."
                + coverage_note
            ),
        }
    peers = info.get("meshPeerGenerations")
    startup = info.get("startupRecord")
    sources: list[str] = []
    if isinstance(peers, list) and peers:
        sources.append(f"mesh peer generations: {', '.join(str(p) for p in peers)}")
    if isinstance(startup, dict):
        sources.append(
            "startup copied-home detection: "
            f"record pid {startup.get('recordPid')} "
            f"generation {startup.get('recordGeneration')}"
        )
    return {
        "name": "duplicate-instance",
        "status": "fail",
        "detail": (
            "a duplicate live daemon instance of this node identity was "
            f"detected ({'; '.join(sources)}); detection and alarm only -- "
            "no process is killed or taken offline automatically."
            + coverage_note
        ),
        "action": {
            "command": "hyprial ps --json",
            "description": (
                "Inspect duplicateInstance, find the second daemon process "
                "(a copied HYPRIAL_HOME is the known cause), and stop it by "
                "PID. Automatic remediation is deliberately not performed."
            ),
        },
    }


def _dsh_host_describe(endpoint: str, *, timeout_seconds: float = 2.0) -> object:
    """Actively probe one DSH endpoint through its public HTTP API."""

    import asyncio
    import threading

    from hyprial.daemon import DshHttpApi

    api = DshHttpApi(endpoint, timeout_seconds=timeout_seconds)
    # wait_for alone cannot stop to_thread socket/DNS I/O. The transport's
    # close fence aborts both, including a server that trickles response bytes.
    deadline = threading.Timer(timeout_seconds, api.close)
    deadline.daemon = True
    deadline.start()
    try:
        return asyncio.run(api.call("host.describe", {}))
    finally:
        deadline.cancel()
        api.close()


def _dsh_endpoint_label(endpoint: str) -> str:
    """Diagnostics never print credentials, query strings or deployment paths."""

    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "[invalid endpoint]"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        path = "/[redacted-path]" if parsed.path.strip("/") else ""
        return f"{parsed.scheme}://{host}{port}{path}"
    except ValueError:
        return "[invalid endpoint]"


def _dsh_doctor_check(result: JsonObject) -> JsonObject | None:
    """Probe every self-launched DSH endpoint reported by status.

    The endpoint is an output of the worker's current generation, so a worker
    without one has not readied a child yet; liveness alone is not evidence
    that its ``/api`` works.
    """
    services = get_services()

    raw_connectors = result.get("connectors")
    if not isinstance(raw_connectors, list):
        return None
    connectors = [
        item
        for item in raw_connectors
        if isinstance(item, dict) and item.get("runtime") == "dsh"
    ]
    if not connectors:
        return None

    missing = sorted(
        str(item.get("name"))
        for item in connectors
        if not isinstance(item.get("endpoint"), str) or not item.get("endpoint")
    )
    endpoints = sorted({
        item["endpoint"].rstrip("/") for item in connectors
        if isinstance(item.get("endpoint"), str) and item["endpoint"]
    })
    labels = {endpoint: _dsh_endpoint_label(endpoint) for endpoint in endpoints}
    unreachable: dict[str, str] = {}
    not_ready: list[str] = []
    unprobed: list[str] = []
    # A large roster must not multiply the command's network wait by N.
    deadline = services.time.monotonic() + 4.0
    for endpoint in endpoints:
        remaining = deadline - services.time.monotonic()
        if remaining <= 0:
            unprobed.append(endpoint)
            continue
        try:
            description = services._dsh_host_describe(endpoint, timeout_seconds=min(2.0, remaining))
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            # HTTP error bodies and exception strings can contain credentials.
            unreachable[endpoint] = type(error).__name__
            continue
        if (
            not isinstance(description, dict)
            or not isinstance(description.get("provider"), str)
            or not description["provider"].strip()
            or not isinstance(description.get("model"), str)
            or not description["model"].strip()
        ):
            not_ready.append(endpoint)

    metrics: JsonObject = {
        "endpoints": [labels[e] for e in endpoints],
        "reachable": [labels[e] for e in endpoints if e not in unreachable and e not in unprobed],
        "unreachable": [labels[e] for e in sorted(unreachable)],
        "missingModelConfiguration": [labels[e] for e in not_ready],
        "unprobed": [labels[e] for e in unprobed],
        "missingEndpoint": missing,
    }
    action = {
        "command": "hyprial doctor --json",
        "description": (
            "Inspect the worker's self-launched DSH child (its status endpoint "
            "and dshHome, plus the child io log) and model configuration, then "
            "rerun the probe."
        ),
    }
    if unreachable:
        details = "; ".join(
            f"{labels[endpoint]}: {unreachable[endpoint]}" for endpoint in sorted(unreachable)
        )
        return {
            "name": "dsh-endpoint",
            "status": "fail",
            "detail": f"DSH endpoint unreachable: {details}",
            "action": action,
            "metrics": metrics,
        }
    if not_ready:
        return {
            "name": "dsh-endpoint",
            "status": "fail",
            "detail": (
                "DSH endpoint is reachable but host.describe did not expose a "
                f"configured model route: {', '.join(labels[e] for e in not_ready)}"
            ),
            "action": action,
            "metrics": metrics,
        }
    if missing or unprobed:
        return {
            "name": "dsh-endpoint",
            "status": "warn",
            "detail": (
                "DSH probe incomplete: a worker has no live self-launched "
                "generation reporting an endpoint, or the total probe budget "
                "was exhausted"
            ),
            "action": action,
            "metrics": metrics,
        }
    return {
        "name": "dsh-endpoint",
        "status": "ok",
        "detail": "DSH host.describe reachable with model configuration; inference, credentials and quota were not tested",
        "metrics": metrics,
    }


def _historical_inbox_doctor_check(result: JsonObject) -> JsonObject | None:
    """Warn when same-name messages remain under another node identity."""

    raw_entries = result.get("historicalInboxRecipients")
    if not isinstance(raw_entries, list) or not raw_entries:
        return None
    entries = [
        item
        for item in raw_entries
        if isinstance(item, dict)
        and isinstance(item.get("recipient"), str)
        and isinstance(item.get("currentRecipient"), str)
        and isinstance(item.get("pending"), int)
        and not isinstance(item.get("pending"), bool)
        and item["pending"] > 0
    ]
    if not entries:
        return None
    return {
        "name": "historical-inbox-recipients",
        "status": "warn",
        "detail": (
            "pending messages use same-name agent URIs from another node id; "
            "identity continuity is not proven, so hyprial will not consume them "
            "through the current actor automatically"
        ),
        "action": {
            "command": "hyprial ps --json",
            "description": (
                "Review historicalInboxRecipients, then use the exact old URI "
                "with read/ack only after confirming that the node rename was "
                "intentional."
            ),
        },
        "metrics": {
            "recipients": len(entries),
            "pending": sum(int(item["pending"]) for item in entries),
            "historicalRecipients": [item["recipient"] for item in entries],
            "currentRecipients": [item["currentRecipient"] for item in entries],
        },
    }


def _workflow_worker_cleanup_doctor_check(
    result: JsonObject,
) -> JsonObject | None:
    """Name every terminal worker whose reclaim could not be confirmed."""

    cleanup = result.get("workflowWorkerCleanup")
    raw_findings = cleanup.get("attention") if isinstance(cleanup, dict) else None
    if not isinstance(raw_findings, list):
        return None
    findings = [
        item
        for item in raw_findings
        if isinstance(item, dict)
        and isinstance(item.get("graphId"), str)
        and isinstance(item.get("actor"), str)
        and isinstance(item.get("operationId"), str)
        and isinstance(item.get("ageMs"), int)
        and not isinstance(item.get("ageMs"), bool)
    ]
    if not findings:
        return None
    first = findings[0]
    return {
        "name": "workflow-worker-cleanup",
        "status": "warn",
        "detail": (
            f"{len(findings)} terminal workflow worker(s) need attention; "
            f"graph={first['graphId']} actor={first['actor']} "
            f"operation={first['operationId']} ageMs={first['ageMs']} "
            "lastObservation="
            + json.dumps(first.get("lastObservation"), sort_keys=True)
        ),
        "action": {
            "command": f"hyprial workflow status {first['graphId']} --json",
            "description": (
                "Verify the exact process identity and resolve the lifecycle "
                "failure before treating the worker as reclaimed."
            ),
        },
        "metrics": {"count": len(findings), "workers": findings},
    }


def _pac_gc_doctor_check(result: JsonObject) -> JsonObject | None:
    """Project collector recency and the backlog its last pass left."""

    raw = result.get("pacGc")
    if not isinstance(raw, dict):
        return None
    last_pass = raw.get("lastPassAtMs")
    removed = raw.get("lastRemovedCount")
    backlog = raw.get("removableBacklog")
    if (
        (last_pass is not None and type(last_pass) is not int)
        or type(removed) is not int
        or (backlog is not None and type(backlog) is not int)
    ):
        return None
    metrics = {
        "lastPassAtMs": last_pass,
        "lastRemovedCount": removed,
        "removableBacklog": backlog,
    }
    if last_pass is None:
        return {
            "name": "pac-gc",
            "status": "warn",
            "detail": (
                "PAC GC has not completed a pass; "
                "run `hyprial workflow gc --dry-run` for the current backlog"
            ),
            "metrics": metrics,
        }
    return {
        "name": "pac-gc",
        "status": "ok",
        "detail": (
            f"last pass at {last_pass} removed {removed}; "
            f"{backlog} worker agent(s) still removable after it"
        ),
        "metrics": metrics,
    }


def _mcp_channel_doctor_check(result: JsonObject) -> JsonObject | None:
    """Report code generation/fencing, never infer stdio process liveness."""

    from hyprial.kernel import CHANNEL_PROTOCOL_VERSION, channel_generation

    raw_sessions = result.get("interactiveSessions")
    if not isinstance(raw_sessions, list):
        return None
    channels = [
        item
        for item in raw_sessions
        if isinstance(item, dict)
        and (
            item.get("source") == "claude-channel"
            or item.get("runtime") == "claude_interactive"
        )
    ]
    if not channels:
        return None
    generations = [
        channel_generation(
            build_version=item.get("channelBuildVersion"),
            protocol_version=item.get("channelProtocolVersion"),
        )
        for item in channels
    ]
    legacy = generations.count("legacy_or_unknown")
    unfenced = sum(item.get("ownerFence") is not True for item in channels)
    confirmed = sum(
        item.get("channelCurrentThisGeneration") is True
        and isinstance(item.get("channelCurrentEpoch"), str)
        for item in channels
    )
    confirmed_current = sum(
        generation == "current"
        and item.get("channelCurrentThisGeneration") is True
        and isinstance(item.get("channelCurrentEpoch"), str)
        for item, generation in zip(channels, generations, strict=True)
    )
    alive = sum(item.get("channelAlive") is True for item in channels)
    recently_observed = sum(
        item.get("channelRecentlyObserved") is True for item in channels
    )
    metrics: JsonObject = {
        "total": len(channels),
        "current": confirmed_current,
        "telemetryCurrent": generations.count("current"),
        "notCurrentThisGeneration": len(channels) - confirmed,
        "legacyOrUnknown": legacy,
        "unfenced": unfenced,
        "protocolVersion": CHANNEL_PROTOCOL_VERSION,
        "recentlyObserved": recently_observed,
        "alive": alive,
    }
    if legacy or unfenced or confirmed != len(channels) or alive != len(channels):
        return {
            "name": "mcp-channel-generation",
            "status": "warn",
            "detail": (
                "one or more persisted interactive Claude registrations use "
                "legacy/unknown Channel code, lack the stable owner fence, or "
                "have not completed a ref-guarded refresh and recent "
                "operational-liveness observation for this daemon generation"
            ),
            "action": {
                "command": "hyprial start claude --name <name> [--resume <session-id>]",
                "description": (
                    "Start or resume the coordinator through the upgraded hyprial "
                    "launcher to generate a current, fenced Channel process."
                ),
            },
            "metrics": metrics,
        }
    return {
        "name": "mcp-channel-generation",
        "status": "ok",
        "detail": (
            "interactive Claude registrations report the current Channel "
            "telemetry and owner fence, and hold a recently observed "
            "operational-liveness lease in this daemon generation"
        ),
        "metrics": metrics,
    }


def _lark_inbound_doctor_check(result: JsonObject) -> JsonObject | None:
    """Summarize desired Lark adapters using inbound-stream health."""

    from hyprial.daemon import contracts_lifecycle as lark_lifecycle
    from hyprial.kernel import lark_recovery_coverage

    raw_adapters = result.get("adapters")
    if not isinstance(raw_adapters, list):
        return None
    adapters = [
        item
        for item in raw_adapters
        if isinstance(item, dict)
        and item.get("provider") == "lark"
        and item.get("desired") is True
    ]
    if not adapters:
        return None
    stale = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("streamHealth") == "stale" or item.get("status") == "stale"
    )
    unavailable = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("online") is not True
        and item.get("status") not in {"starting", "checking", "stale"}
    )
    unknown = sorted(
        str(item.get("name"))
        for item in adapters
        if item.get("online") is True
        and item.get("streamHealth") not in {"healthy", "stale"}
    )
    metrics: JsonObject = {
        "stale": stale,
        "unavailable": unavailable,
        "unknown": unknown,
        **lark_recovery_coverage(),
    }
    if stale:
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": (
                "Lark worker process is alive but its inbound websocket is stale"
            ),
            "action": {
                "command": f"hyprial adapter status {stale[0]} --json",
                "description": (
                    "Inspect the active probe and automatic rebuild transition."
                ),
            },
            "metrics": metrics,
        }
    if unavailable:
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": "one or more desired Lark inbound adapters are unavailable",
            "action": {
                "command": f"hyprial adapter status {unavailable[0]} --json",
                "description": "Inspect adapter startup or restart diagnostics.",
            },
            "metrics": metrics,
        }
    over_deadline = sorted(
        (
            str(item.get("name")),
            str(item.get("lifecycleCorrelationId") or "unknown"),
            int(item.get("lifecycleAgeSeconds") or 0),
        )
        for item in adapters
        if item.get("lifecycleCorrelationId")
        and int(item.get("lifecycleAgeSeconds") or 0)
        > lark_lifecycle.START_DEADLINE_SECONDS
    )
    if over_deadline:
        name, correlation, age = over_deadline[0]
        return {
            "name": "lark-inbound",
            "status": "fail",
            "detail": (
                f"Lark adapter {name} lifecycle transition {correlation} has "
                f"held the lifecycle lock for {age}s, past the "
                f"{lark_lifecycle.START_DEADLINE_SECONDS:.0f}s deadline"
            ),
            "action": {
                "command": f"grep '{correlation}' ~/.hyprial/state/logs/daemon.jsonl",
                "description": (
                    "The daemon fails loud at the deadline and releases the "
                    "lock; grep the locking correlation in daemon.jsonl to "
                    "see which command wrote it and where its completion "
                    "settled, then re-run start."
                ),
            },
            "metrics": metrics,
        }
    if any(item.get("status") in {"starting", "checking"} for item in adapters):
        return {
            "name": "lark-inbound",
            "status": "warn",
            "detail": (
                "one or more desired Lark inbound adapters are starting "
                "or verifying history"
            ),
            "metrics": metrics,
        }
    if unknown:
        return {
            "name": "lark-inbound",
            "status": "warn",
            "detail": "Lark worker is online but did not report inbound telemetry",
            "action": {
                "command": f"hyprial adapter status {unknown[0]} --json",
                "description": "Inspect the worker version and telemetry reader.",
            },
            "metrics": metrics,
        }
    return {
        "name": "lark-inbound",
        "status": "ok",
        "detail": (
            "desired Lark inbound event streams are live; history recovery "
            "covers known chats only, unknown first-chat recovery is unsupported, "
            "and chat enumeration is not implemented"
        ),
        "metrics": metrics,
    }


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Run read-only health checks."""
    services = get_services()

    services._execute(lambda: _doctor_result(), json_output=json_output)
