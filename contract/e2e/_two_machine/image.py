from __future__ import annotations

import sys
from collections.abc import Mapping

from . import tailcat


def labeled_image_entries(ports, ) -> list[tuple[str, str]]:
    """``(createdAt, fullId)`` for every image carrying the inputs label,
    dangling ones included (a dangling image is just an overwritten tag,
    and it keeps its labels).

    Selection is keyed on ``hyprial.e2e001.inputs`` — the label only this
    runner's builds carry — never on the broader ``hyprial-e2e001=1``
    (批复 §1b): an image missing the inputs label must never be a sweep
    candidate, no matter what other labels it has.
    """

    short_ids = [
        item for item in ports.docker(
            ["images", "-a", "-q", "--filter", f"label={ports.INPUTS_LABEL}"],
            timeout=30.0,
        ).split() if item
    ]
    entries: list[tuple[str, str]] = []
    for short_id in short_ids:
        line = ports.docker(
            ["image", "inspect", short_id, "--format", "{{.Id}} {{.Created}}"],
            timeout=30.0,
        )
        full_id, _, created = line.partition(" ")
        entries.append((created, full_id))
    return entries

def prefixed_networks(ports, ) -> list[str]:
    probe = ports.sh(["docker", "network", "ls", "-q", "--filter", f"name={ports.PREFIX}"], check=False)
    return [item for item in probe.stdout.split() if item]

def run_docker(ports, *, rebuild: bool) -> None:
    ports.docker_available()
    ports.pre_clean()
    ports.build_image(force=rebuild)
    ports.docker(["network", "create", "--internal", "--label", "hyprial-e2e001=1", ports.NETWORK], timeout=60.0)
    ports.start_relay()
    ports.run_container(ports.NODES[0])
    ports.run_container(ports.NODES[1])
    ports.wait_daemon_ready(ports.NODES[0])
    ports.wait_daemon_ready(ports.NODES[1])
    ports.register_driver(ports.NODES[0])
    ports.register_driver(ports.NODES[1])

    org_space = ports.create_org_space(
        ports.NODES[0], prefix="e2e001", actor=ports.driver_uri(ports.NODES[0])
    )
    ports.invite_org_owner(
        ports.NODES[0], org_space, ports.owner_of_node(ports.NODES[1])
    )
    ports.join_org_space(
        ports.NODES[1], org_space, actor=ports.driver_uri(ports.NODES[1])
    )
    print(f"RESULT ok phase0 both containers share org space {org_space}")

    ports.wait_for_target(ports.NODES[0], ports.NODES[1])
    ports.wait_for_target(ports.NODES[1], ports.NODES[0])
    for node in ports.NODES:
        ports.assert_managed_endpoints(node, ports.exec_json(node, "ps"))
    # Phase 1 sends to the advertised targetUri verbatim: the registered
    # driver's canonical URI is a real identity, and the copy-paste path
    # delivers.
    advertised_b = ports.advertised_target(ports.NODES[0], ports.driver_uri(ports.NODES[1]))
    if not advertised_b.startswith("agent:"):
        raise AssertionError(f"advertised targetUri lost its agent: form: {advertised_b}")
    ports.roundtrip(
        "phase1",
        ports.NODES[0],
        ports.driver_uri(ports.NODES[0]),
        ports.NODES[1],
        ports.driver_uri(ports.NODES[1]),
        "CONTRACT_E2E_REQUEST_1",
        receiver_target=advertised_b,
    )

    offline_message_id = ports.offline_queue(ports.NODES[0], ports.driver_uri(ports.NODES[0]), ports.NODES[1], ports.driver_uri(ports.NODES[1]))

    # Phase 3: restart self-heal, including a message sent inside the
    # restart window: B queues it while A is down, and it must arrive after
    # A returns.
    print(f"RESULT info stopping {ports.NODES[0]} (self-heal phase)")
    ports.sh(["docker", "stop", ports.NODES[0]], timeout=30.0)
    window_id = ports.queue_while_unreachable(
        "phase3",
        ports.NODES[1],
        ports.driver_uri(ports.NODES[1]),
        ports.driver_uri(ports.NODES[0]),
        f"CONTRACT_E2E_WINDOW_{int(ports.time.time())}",
    )
    ports.sh(["docker", "start", ports.NODES[0]], timeout=30.0)
    ports.wait_daemon_ready(ports.NODES[0])
    # Phase 3's restart is the second half of the restart-survival proof:
    # endpoints must come back from the launcher environment re-read by the
    # re-run entrypoint (206876ed contract), before any healing is credited.
    ports.assert_managed_endpoints(ports.NODES[0], ports.exec_json(ports.NODES[0], "ps"))
    print(
        f"RESULT ok phase3 {ports.NODES[0]} endpoints active after restart "
        "(launcher env re-read by entrypoint)"
    )
    ports.wait_for_target(ports.NODES[0], ports.NODES[1])
    ports.wait_for_target(ports.NODES[1], ports.NODES[0])
    ports.assert_delivered_once("phase3", ports.NODES[1], ports.NODES[0], ports.driver_uri(ports.NODES[0]), window_id)
    print("RESULT ok phase3 restart-window message delivered after self-heal")
    ports.roundtrip(
        "phase3", ports.NODES[1], ports.driver_uri(ports.NODES[1]), ports.NODES[0], ports.driver_uri(ports.NODES[0]), "CONTRACT_E2E_REQUEST_3"
    )

    ports.suspend_resume("phase4", ports.NODES[0], ports.driver_uri(ports.NODES[0]), ports.NODES[1], ports.driver_uri(ports.NODES[1]))
    ports.mesh_reconnect("phase5", ports.NODES[0], ports.driver_uri(ports.NODES[0]), ports.NODES[1], ports.driver_uri(ports.NODES[1]))
    # Last, so the third container cannot perturb any of the phases above.
    ports.delivery_terminal_state(offline_message_id)
    # Later still: once a mailbox exists, EVERY send to an offline recipient
    # transfers custody instead of queueing on the sender, which is exactly
    # what phases 2-5 assert does not happen.
    ports.mailbox_custody(ports.NODES[1])

def remove_resources(ports, ) -> list[str]:
    """Remove every `{PREFIX}` container, network and labeled volume.

    Each step swallows errors so a failure here is surfaced by the self-check
    instead of silently stopping the rest of the cleanup.
    """

    problems: list[str] = []
    containers = ports.prefixed_containers()
    if containers:
        try:
            ports.docker(["rm", "-f", *containers], timeout=120.0)
        except ports.DockerError as error:
            problems.append(f"containers: {error}")
    networks = ports.prefixed_networks()
    if networks:
        for network in networks:
            try:
                ports.docker(["network", "rm", network], timeout=60.0)
            except ports.DockerError as error:
                problems.append(f"network {network}: {error}")
    volumes = ports.prefixed_volumes()
    if volumes:
        for volume in volumes:
            try:
                ports.docker(["volume", "rm", volume], timeout=60.0)
            except ports.DockerError as error:
                problems.append(f"volume {volume}: {error}")
    # Reap labeled images beyond the newest two (never the currently tagged
    # image, which the sweep protects).  This also catches dangling layers an
    # ABORTED build left behind — the path the old image-prune covered.
    try:
        protect = None
        try:
            protect = ports.docker(["image", "inspect", ports.IMAGE, "--format", "{{.Id}}"], timeout=30.0)
        except ports.DockerError:
            pass
        ports.sweep_old_labeled_images(keep=2, protect=protect)
    except ports.DockerError as error:
        problems.append(f"image sweep: {error}")
    return problems

def image_inputs_label(ports, ) -> str | None:
    """The inputs label on the current fixed-tag image, None when absent.

    Raises DockerError when the image itself does not exist — callers treat
    that as "must build".
    """

    value = ports.docker(
        [
            "image",
            "inspect",
            ports.IMAGE,
            "--format",
            f"{{{{index .Config.Labels \"{ports.INPUTS_LABEL}\"}}}}",
        ],
        timeout=30.0,
    )
    return value or None

def docker_available(ports, ) -> None:
    if ports.shutil.which("docker") is None:
        raise ports.DockerError("docker binary not found; use --local for the loopback sim")
    ports.sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30.0)

def self_check(ports, ) -> list[str]:
    """Assert no `{PREFIX}` container/network and no labeled volume survived."""

    leftovers: list[str] = []
    containers = ports.prefixed_containers()
    if containers:
        leftovers.append(f"containers {containers}")
    networks = ports.prefixed_networks()
    if networks:
        leftovers.append(f"networks {networks}")
    volumes = ports.prefixed_volumes()
    if volumes:
        leftovers.append(f"volumes {volumes}")
    return leftovers

def run_container(ports, 
    node: str, *, extra_env: dict[str, str] | None = None
) -> None:
    environment = ports.container_environment(node, extra_env=extra_env)
    flags: list[str] = []
    for name, value in environment.items():
        flags.extend(["-e", f"{name}={value}"])
    ports.docker(
        [
            "run",
            "-d",
            "--name",
            node,
            "--network",
            ports.NETWORK,
            *flags,
            "--label",
            "hyprial-e2e001=1",
            ports.IMAGE,
        ],
        timeout=120.0,
    )
    print(f"RESULT info {node} started (private Unix backend, real Tailcat sidecar)")

def pre_clean(ports, ) -> None:
    """Remove leftovers from a previous abnormal exit before creating anything."""

    problems = ports.remove_resources()
    if problems:
        raise ports.DockerError(f"pre-clean could not remove leftover resources: {problems}")

def sweep_old_labeled_images(ports, *, keep: int = 2, protect: str | None = None) -> list[str]:
    """Keep the newest ``keep`` labeled images plus the protected (current)
    one; remove older labeled images (spec-e2e001 追加 1.3).

    Only images carrying this runner's label are ever candidates — no global
    prune, no builder cache, nothing of other tenants on the shared VM.
    Replaces the previous ``image prune --filter label=…``, which deleted
    every dangling labeled image and thereby made "keep the last two"
    impossible.
    """

    entries = ports.labeled_image_entries()
    entries.sort(reverse=True)  # RFC3339 timestamps sort chronologically
    # The current image never counts against the quota: 追加 1.3 keeps the
    # newest two OLD tree images in addition to the one in use.
    remainder = [entry for entry in entries if not ports._is_protected(entry, protect)]
    doomed = remainder[keep:]
    removed: list[str] = []
    for _created, full_id in doomed:
        ports.docker(["rmi", "-f", full_id], timeout=120.0)
        removed.append(full_id[:19])
    return removed

def prefixed_containers(ports, ) -> list[str]:
    probe = ports.sh(["docker", "ps", "-aq", "--filter", f"name={ports.PREFIX}"], check=False)
    return [item for item in probe.stdout.split() if item]

def container_environment(ports, 
    node: str, *, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Every E2E-001 container's environment, built the way _daemon_runner
    builds IsolatedDaemon's: the isolation defines the identity.

    #361 made the node's owner mandatory (HYPRIAL_OWNER, else settings.json
    "owner", else the daemon refuses to start) and U5 removed `init --owner`;
    the container mounts a fresh HYPRIAL_HOME with no settings.json, so
    without HYPRIAL_OWNER the daemon never comes up ("user identity is not
    set" -- isolated-macos task 9379/9294, red on every nightly since).
    The value is fixed, never inherited from the host shell: GHOST_ACTOR
    already mints `agent:e2e:...`, and driver_uri reads the owner back from
    the daemon's own report, so every wire identity stays on `e2e`.  A phase
    needing a different owner passes it via extra_env, which merges last and
    therefore wins (the same rule _daemon_runner documents).
    """
    environment = {
        "HYPRIAL_OWNER": "e2e",
        "HYPRIAL_NODE_ID": node,
        "HYPRIAL_HOME": "/data/home",
        "HARNESS_STATE_DIR": "/data/state",
        **tailcat.environment(ports),
        **(extra_env or {}),
    }
    if ports._ci_capture_enabled():
        environment["HYPRIAL_E2E_TRACE_PRESENCE"] = "1"
    return environment

def prefixed_volumes(ports, ) -> list[str]:
    probe = ports.sh(
        ["docker", "volume", "ls", "-q", "--filter", "label=hyprial-e2e001=1"],
        check=False,
    )
    return [item for item in probe.stdout.split() if item]

def proxy_build_args(ports, environ: Mapping[str, str], platform: str = sys.platform) -> list[str]:
    """``--build-arg`` pairs that route the build's pip/uv through the host proxy.

    The image build's ``RUN pip install uv && uv sync`` otherwise goes straight
    to PyPI, and on the macOS host runner that path times out (measured
    2026-09-27: direct ReadTimeout after 86 s vs 12 s through the proxy; task
    35833's E2E-001 failed this way).  The proxy names are Docker's predefined
    ARGs: they are neither part of the layer-cache key nor persisted in the
    image, so passing them changes neither reuse nor the inputs label.

    A loopback proxy (e2e-macos-isolated.yml exports ``http://127.0.0.1:7896``) is the job
    host's own loopback; inside the build container it has to be spelled
    ``host.docker.internal``.  That name resolves by default only under Docker
    Desktop / OrbStack on macOS (the one runner that runs E2E-001, macos-host).
    Elsewhere a loopback proxy is unreachable from the build container either
    way, so it is not passed at all: the build keeps its previous direct path
    instead of trading it for a dead proxy.
    """

    args: list[str] = []
    for name in ports.PROXY_BUILD_ARGS:
        value = environ.get(name)
        if not value:
            continue
        if not name.lower().startswith("no_"):
            match = ports._LOOPBACK_PROXY.match(value)
            if match:
                if platform != "darwin":
                    continue
                value = f"{match['scheme']}host.docker.internal{match['rest'] or ''}"
        args += ["--build-arg", f"{name}={value}"]
    return args


def _ca_build_args(ports, environ: Mapping[str, str]) -> list[str]:
    """Mount optional build trust without persisting certificates in the image."""

    configured = environ.get("HYPRIAL_E2E_DOCKER_CA_BUNDLE")
    if not configured:
        return []
    try:
        certificate = ports.Path(configured).expanduser().resolve(strict=True)
    except OSError as error:
        raise ports.DockerError(f"Docker CA bundle is unavailable: {configured}") from error
    if not certificate.is_file() or "," in str(certificate):
        raise ports.DockerError(f"Docker CA bundle must be a regular file without commas: {certificate}")
    return ["--secret", f"id=hyprial_e2e_ca,src={certificate}"]


def build_image(ports, *, force: bool) -> None:
    """Build (or reuse) the fixed-tag image, keyed on its build inputs.

    Reuse requires ALL of: no --rebuild, no dirty build inputs (the label
    describes the tested commit, and a dirty tree builds content the label
    would not stand behind), and an existing image whose inputs label equals
    the hash of the tree under test.  Anything else rebuilds.
    """

    hash_value, exact = ports.inputs_hash(ports.ROOT)
    description = ports.describe_inputs(hash_value, exact=exact)
    dirty = ports.dirty_inputs(ports.ROOT)
    current_label: str | None = None
    if not force and not dirty:
        try:
            current_label = ports.image_inputs_label()
        except ports.DockerError:
            current_label = None
    if not force and not dirty and current_label == hash_value:
        print(f"RESULT ok image {ports.IMAGE} present ({description} matched)")
        return
    if force:
        reason = "forced by --rebuild"
    elif dirty:
        reason = f"working tree dirty for image inputs; {description} describes HEAD"
    elif current_label is not None:
        reason = f"{description} != existing label {current_label or 'absent'}"
    else:
        reason = f"no reusable image ({description})"
    print(f"RESULT info building {ports.IMAGE} — {reason}; first run pulls the base image")
    # A dirty build may contain content its label cannot describe, so it
    # gets a suffix that never matches a clean computation: once the tree is
    # clean again, the mismatch forces a rebuild instead of silently reusing
    # an image built from different content.
    stamped_label = f"{ports.INPUTS_LABEL}={hash_value}+dirty" if dirty else f"{ports.INPUTS_LABEL}={hash_value}"
    ports.docker(
        [
            "build",
            "-t",
            ports.IMAGE,
            "--label",
            stamped_label,
            *ports.proxy_build_args(ports.os.environ),
            *_ca_build_args(ports, ports.os.environ),
            "-f",
            str(ports.ROOT / "contract/e2e/docker/Dockerfile"),
            str(ports.ROOT),
        ],
        timeout=900.0,
    )
    # The old fixed-tag image is now dangling; keep the newest two labeled
    # images (this one plus its predecessor) and reap the rest (追加 1.3).
    current_id = ports.docker(["image", "inspect", ports.IMAGE, "--format", "{{.Id}}"], timeout=30.0)
    removed = ports.sweep_old_labeled_images(keep=2, protect=current_id)
    print(
        f"RESULT info image sweep: kept newest 2 labeled, "
        f"removed {removed if removed else 'nothing'}"
    )

def _is_protected(ports, entry: tuple[str, str], protect: str | None) -> bool:
    if not protect:
        return False
    full_id = entry[1]
    return full_id.startswith(protect) or protect.startswith(full_id)
