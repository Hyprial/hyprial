# Decision record (file header; no inline comments by repository rule):
# - Sequence: resolve the daemon's authorized env credentials, seal every
#   credential under the target's public key, export the home with the sealed
#   files held back, then pack the bundle directory into one tar.
# - No attribution signature is stamped: the target lands with
#   an unsigned landing policy (protocol §0, KP-047).
# - The launch identity bag is the teleport's own, so a landed row resolves to
#   a launch.
# - Heavy imports are deferred to the call: importing this module (through the
#   request catalog) must not load the transfer stack.
# - Carried org exclusions preserved in <agent_home>/state/org-agent-exclusions.json
#   are merged with local exclusions and written via ``export_bundle(...,
#   replace_files=...)`` so re-export avoids payload collisions while keeping
#   ``exclude_paths`` strictly disjoint from ``extra_files`` for credentials.
"""Export a quiesced agent home into a sealed, packed bundle stream."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.teleport.source import ExportedStream, SourceAgent
from hyprial.kernel import HarnessLaunchSpec

BUNDLE_DIRNAME = "bundle"
STREAM_NAME = "bundle.tar"


def export_agent_stream(
    app: Any,
    agent: SourceAgent,
    spec_json: Any,
    *,
    recipient_public_key: bytes,
    workdir: Path,
) -> ExportedStream:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        TRANSFER_EXCLUSIONS_RELPATH,
        export_agent_exclusions_bytes,
    )
    from hyprial.daemon.impl.teleport.stream import pack_bundle
    from hyprial.daemon.impl.teleport.wiring.source_ops import launch_wiring_identity
    from hyprial.daemon.impl.transfer.archive.bundle import (
        MANIFEST_DIGEST_FIELD,
        export_bundle,
    )
    from hyprial.daemon.impl.transfer.archive.env_credentials import (
        env_credentials_for_quiesce,
    )
    from hyprial.daemon.impl.transfer.archive.envelope import (
        ENVELOPE_RELPATH,
        EnvCredential,
        envelope_json_bytes,
        plan_credential_seal,
        seal_credential_envelope,
    )

    if agent.home is None:
        raise RuntimeError(f"agent {agent.name!r} has no home to export")
    spec = HarnessLaunchSpec.from_json(spec_json, "spec")
    record = app.agents.require(agent.name)
    home = Path(agent.home)
    env_credentials = tuple(
        EnvCredential.from_dict(item)
        for item in env_credentials_for_quiesce(
            registry=app._agent_registry,
            hyprial_home=app.hyprial_home,
            spec=spec,
            app=app,
        )
    )
    seal_plan = plan_credential_seal(home, env_credentials=env_credentials)
    created_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    envelope = seal_credential_envelope(
        seal_plan,
        actor=agent.name,
        owner=str(app.owner),
        created_at=created_at,
        recipient_public_key=recipient_public_key,
    )
    extra_files: dict[str, tuple[bytes, int]] = {
        ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600),
    }
    exclusions_bytes = export_agent_exclusions_bytes(
        Path(app.hyprial_home), record.agent_id, agent_home=home
    )
    replace_files: dict[str, tuple[bytes, int]] = {}
    if exclusions_bytes is not None:
        replace_files[TRANSFER_EXCLUSIONS_RELPATH] = (exclusions_bytes, 0o600)
    bundle_dir = workdir / BUNDLE_DIRNAME
    manifest = export_bundle(
        home,
        bundle_dir,
        owner=str(app.owner),
        actor=agent.name,
        source_machine=str(app.node_id),
        created_at=created_at,
        session_refs=(agent.session_ref,) if agent.session_ref else (),
        source_epoch=record.entity_token,
        identity=launch_wiring_identity(record.to_json(), agent.harness),
        credential_envelope=ENVELOPE_RELPATH,
        exclude_paths=seal_plan.excluded,
        extra_files=extra_files,
        replace_files=replace_files,
    )
    packed = pack_bundle(bundle_dir, workdir / STREAM_NAME)
    return ExportedStream(
        path=packed.path,
        total=packed.total,
        sha256=packed.sha256,
        manifest_digest=str(manifest.as_dict()[MANIFEST_DIGEST_FIELD]),
    )
