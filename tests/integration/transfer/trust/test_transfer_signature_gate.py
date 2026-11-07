"""AT06 slice 2b: the landing-side attribution gate, on every landing entry.

The gate is the seam between ``validate_bundle`` (integrity) and the first
write (attribution).  These arms are red-first: the four refusal codes each
have to leave the target byte-identical, the seal has to die first, and the
three production entries have to be fail-closed without a signature.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleTamperError,
    export_bundle,
    load_manifest,
)
from hyprial.daemon.impl.transfer.manifest_reading import manifest_digest
from hyprial.daemon.impl.transfer.archive.envelope import (
    envelope_header_digest,
    generate_keypair,
    seal_envelope,
)
from hyprial.daemon.impl.transfer.archive.signature import (
    SignatureFormatError,
    SignatureInvalidError,
    SignatureKeyError,
    SignatureMissingError,
    generate_signing_key,
    public_key_id,
    sign_bundle,
)
from hyprial.daemon.impl.transfer.archive.trust import (
    RETIREMENT_REFUSE,
    RETIREMENT_WARN,
    TrustStore,
    TrustedSourceKey,
)
from hyprial.daemon.impl.transfer.landing.owner import land_agent_home
from hyprial.daemon.impl.transfer.landing.receive import receive_bundle
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.daemon.impl.transfer.landing.signature_gate import (
    SIGNATURE_NAME,
    SignatureIdentityMismatch,
    SignaturePolicy,
    SignatureRetiredError,
    require_trusted_signature,
    trust_policy_for,
)

CREATED_AT = "2026-10-11T09:00:00+08:00"
OWNER = "owner@example.com"
ACTOR = "codex-sw"
MACHINE = "devusr-dev"
ADDED_AT = "2026-10-11T00:00:00+00:00"
RETIRED_AT = "2026-10-11T06:00:00+00:00"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ENVELOPE_PATH = "credentials/envelope.json"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "agent-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"actor": "demo"}\n')
    (home / "notes.md").write_text("hello bundle\n")
    return home


def _bundle(
    tmp_path: Path,
    *,
    envelope: Path | None = None,
    actor: str = ACTOR,
    machine: str = MACHINE,
) -> Path:
    source = _source_home(tmp_path)
    declared = ""
    if envelope is not None:
        target = source / ENVELOPE_PATH
        target.parent.mkdir(parents=True)
        target.write_text(envelope.read_text())
        declared = ENVELOPE_PATH
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        source,
        bundle_dir,
        owner=OWNER,
        actor=actor,
        source_machine=machine,
        created_at=CREATED_AT,
        session_refs=["session-1"],
        credential_envelope=declared,
    )
    return bundle_dir


def _sign(
    bundle_dir: Path,
    *,
    private_bytes: bytes,
    header_digest: str | None = None,
    digest: str | None = None,
) -> dict[str, object]:
    """Write ``signature.json`` the way the export side will (slice 2c)."""

    block = sign_bundle(
        private_bytes=private_bytes,
        digest=digest or manifest_digest(bundle_dir),
        header_digest=header_digest,
    )
    path = bundle_dir / SIGNATURE_NAME
    path.write_text(json.dumps(block.as_dict(), indent=2) + "\n", encoding="utf-8")
    return block.as_dict()


def _store(
    public_bytes: bytes,
    *,
    actor: str = ACTOR,
    machine: str = MACHINE,
    retired_at: str | None = None,
    retirement_policy: str = RETIREMENT_WARN,
) -> TrustStore:
    return TrustStore(
        keys=(
            TrustedSourceKey.create(
                actor=actor,
                source_machine=machine,
                public_key=public_bytes,
                added_at=ADDED_AT,
                retired_at=retired_at,
            ),
        ),
        retirement_policy=retirement_policy,
    )


def _policy(store: TrustStore | None, *, now: datetime = NOW) -> SignaturePolicy:
    return SignaturePolicy(trust_store=store, now=lambda: now)


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under ``root`` with its bytes and mode, or an empty map."""

    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _Untouchable:
    """Any attribute access is a failure: the gate runs before this is used."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the registry was used before the gate ({name})")


class _Authority:
    """Minimal authority that records every call it is asked to make."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def agent_exists(self, actor: str) -> bool:
        self.calls.append(f"exists:{actor}")
        return False

    def adopt_agent(self, actor: str, **_: object) -> None:
        self.calls.append(f"adopt:{actor}")

    def materialise_grant(self, actor: str, grant: str) -> str | None:
        self.calls.append(f"grant:{grant}")
        return None

    def confirm_landing(self, actor: str) -> None:
        self.calls.append(f"confirm:{actor}")

    def revoke_grants(self, actor: str, grant_ids: object) -> None:
        self.calls.append(f"revoke:{actor}")


def test_a_signed_bundle_lands_and_the_receipt_names_the_signer(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    block = _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"

    result = receive_bundle(
        bundle_dir,
        destination,
        expect_actor=ACTOR,
        signature=_policy(_store(public)),
    )

    # The signature covers the existing canonical seal, not a second digest.
    sealed = json.loads((bundle_dir / "manifest.json").read_text())["manifest_digest"]
    assert block["digest"] == f"sha256:{sealed}" == manifest_digest(bundle_dir)
    assert result.signature is not None
    assert result.signature.key_id == public_key_id(public)
    assert result.signature.actor == ACTOR
    assert result.signature.source_machine == MACHINE
    assert result.signature.header_digest is None
    receipt = result.as_dict()["signature"]
    assert receipt == {
        "keyId": public_key_id(public),
        "actor": ACTOR,
        "sourceMachine": MACHINE,
        "digest": manifest_digest(bundle_dir),
        "headerDigest": None,
        "retirementNotice": None,
    }
    assert (destination / "notes.md").read_text() == "hello bundle\n"


def test_the_receipt_names_the_actor_the_sealed_manifest_claims(
    tmp_path: Path,
) -> None:
    """F2: the attestation is the manifest's own claim, not a store-side label.

    The store names the identity the key is enrolled for; the sealed manifest
    names the identity the bundle lands as.  When they agree, the receipt has to
    record *that* claim -- a receipt that reported the store's copy would be
    right only by accident and would diverge the moment the two did.
    """

    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    manifest = load_manifest(bundle_dir)

    attested = require_trusted_signature(bundle_dir, policy=_policy(_store(public)))

    assert attested is not None
    assert attested.actor == manifest.actor == ACTOR
    assert attested.source_machine == manifest.source_machine == MACHINE


def test_the_gate_refuses_a_key_signing_for_an_actor_it_was_not_enrolled_for(
    tmp_path: Path,
) -> None:
    """F2's whole point: a trusted key may not vouch for a second actor name.

    The signature verifies under the enrolled key (same key, honest bytes), so
    crypto alone cannot tell these bundles apart.  Only the manifest's claim
    against the enrollment can -- and the refusal must name the claim, never
    the identity the target enrolled.
    """

    bundle_dir = _bundle(tmp_path, actor="xfer-src2")
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureIdentityMismatch) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_signature_identity_mismatch"
    assert _snapshot(tmp_path) == before
    assert not destination.exists()
    message = str(caught.value)
    assert "'xfer-src2'" in message
    assert public_key_id(public) in message
    # The enrolled identity is the target's own file, not the sender's to learn.
    assert f"'{ACTOR}'" not in message


def test_the_gate_refuses_a_bundle_claiming_a_machine_it_was_not_enrolled_for(
    tmp_path: Path,
) -> None:
    """The enrollment's identity is a pair; both halves stay bound."""

    bundle_dir = _bundle(tmp_path, machine="some-other-node")
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureIdentityMismatch) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_signature_identity_mismatch"
    assert _snapshot(tmp_path) == before
    assert "some-other-node" in str(caught.value)
    assert f"'{MACHINE}'" not in str(caught.value)


def test_a_mismatched_identity_never_shadows_a_bad_signature(
    tmp_path: Path,
) -> None:
    """Attack order: the crypto is weighed first, so tampering is still tampering.

    A bundle that both re-points the digest and claims an unenrolled actor must
    refuse as an invalid signature; reporting the identity mismatch would let a
    forger probe the target's enrollment for free.
    """

    bundle_dir = _bundle(tmp_path, actor="xfer-src2")
    private, public = generate_signing_key()
    block = _sign(bundle_dir, private_bytes=private)
    block["digest"] = "sha256:" + "ab" * 32
    (bundle_dir / SIGNATURE_NAME).write_text(json.dumps(block))
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureInvalidError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_signature_invalid"
    assert _snapshot(tmp_path) == before


def test_the_owner_landing_never_adopts_a_bundle_under_an_unenrolled_actor(
    tmp_path: Path,
) -> None:
    """The refusal lands before the registry is touched, not after."""

    bundle_dir = _bundle(tmp_path, actor="xfer-src2")
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"
    authority = _Authority()
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureIdentityMismatch) as caught:
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=_policy(_store(public)),
        )

    assert caught.value.code == "bundle_signature_identity_mismatch"
    assert authority.calls == []
    assert _snapshot(tmp_path) == before
    assert not destination.exists()


def test_the_gate_refuses_a_bundle_that_carries_no_signature(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureMissingError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(b"x" * 32)))

    assert caught.value.code == "bundle_signature_missing"
    assert _snapshot(tmp_path) == before
    assert not destination.exists()


def test_the_gate_refuses_a_malformed_signature_block(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"
    (bundle_dir / SIGNATURE_NAME).write_text('{"keyId": "ed25519:nope"}\n')
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureFormatError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(b"x" * 32)))

    assert caught.value.code == "bundle_signature_malformed"
    assert _snapshot(tmp_path) == before


def test_the_gate_refuses_a_key_the_target_was_never_told_about(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    private, _public = generate_signing_key()
    _, other_public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureKeyError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(other_public)))

    assert caught.value.code == "bundle_signature_unknown_key"
    assert _snapshot(tmp_path) == before


def test_the_gate_refuses_a_signature_that_does_not_verify(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    block = _sign(bundle_dir, private_bytes=private)
    # Re-point the block at a different digest: the crypto no longer holds even
    # though the key is trusted and the shape is well formed.
    block["digest"] = "sha256:" + "ab" * 32
    (bundle_dir / SIGNATURE_NAME).write_text(json.dumps(block))
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureInvalidError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_signature_invalid"
    assert _snapshot(tmp_path) == before


def test_the_seal_still_dies_first_so_the_signature_is_never_reached(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    tampered = bundle_dir / "payload" / "notes.md"
    tampered.write_text("rewritten after export\n")

    with pytest.raises(BundleTamperError):
        receive_bundle(
            bundle_dir,
            tmp_path / "target-home",
            signature=_policy(_store(public)),
        )


def test_a_retired_key_still_lands_and_the_notice_is_recorded(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)

    result = receive_bundle(
        bundle_dir,
        tmp_path / "target-home",
        signature=_policy(_store(public, retired_at=RETIRED_AT)),
    )

    assert result.signature is not None
    assert result.signature.retirement_notice is not None
    assert RETIRED_AT in result.signature.retirement_notice


def test_a_refusing_store_turns_a_retired_key_into_a_refusal(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureRetiredError) as caught:
        receive_bundle(
            bundle_dir,
            destination,
            signature=_policy(
                _store(
                    public, retired_at=RETIRED_AT, retirement_policy=RETIREMENT_REFUSE
                )
            ),
        )

    assert caught.value.code == "bundle_signature_retired"
    assert _snapshot(tmp_path) == before


def test_a_signature_over_a_different_envelope_header_is_refused(
    tmp_path: Path,
) -> None:
    _, recipient_public = generate_keypair()
    sealed = seal_envelope(
        {"github": "synthetic"},
        recipient_public_key=recipient_public,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
    )
    envelope_src = tmp_path / "sealed-envelope.json"
    envelope_src.write_text(json.dumps(sealed))
    bundle_dir = _bundle(tmp_path, envelope=envelope_src)
    private, public = generate_signing_key()
    envelope = json.loads((bundle_dir / "payload" / ENVELOPE_PATH).read_text())
    header_digest = envelope_header_digest(envelope)

    _sign(bundle_dir, private_bytes=private, header_digest=header_digest)
    store = _policy(_store(public))
    attested = require_trusted_signature(
        bundle_dir, policy=store, credential_envelope=ENVELOPE_PATH
    )
    assert attested is not None and attested.header_digest == header_digest

    # A second block that covers a different header must not verify, even
    # though every other byte of the bundle is unchanged.
    other = "sha256:" + "cd" * 32
    block = sign_bundle(
        private_bytes=private,
        digest=manifest_digest(bundle_dir),
        header_digest=other,
    )
    (bundle_dir / SIGNATURE_NAME).write_text(json.dumps(block.as_dict()))

    with pytest.raises(SignatureInvalidError) as caught:
        require_trusted_signature(
            bundle_dir, policy=store, credential_envelope=ENVELOPE_PATH
        )

    assert caught.value.code == "bundle_signature_invalid"


@pytest.mark.parametrize("entry", ["receive", "registry", "owner"])
def test_every_landing_entry_refuses_an_unsigned_bundle(
    tmp_path: Path, entry: str
) -> None:
    """The production default is fail-closed on all three doors."""

    bundle_dir = _bundle(tmp_path)
    policy = _policy(_store(b"x" * 32))
    destination = tmp_path / "target-home"
    authority = _Authority()
    before = _snapshot(tmp_path)

    with pytest.raises(SignatureMissingError) as caught:
        if entry == "receive":
            receive_bundle(bundle_dir, destination, signature=policy)
        elif entry == "registry":
            land_bundle_into_registry(
                bundle_dir,
                registry=_Untouchable(),
                home_root=tmp_path / "agents",
                staging=tmp_path / "staging",
                granted_by=f"user:{OWNER}",
                signature=policy,
            )
        else:
            land_agent_home(
                bundle_dir,
                destination,
                authority=authority,
                signature=policy,
            )

    assert caught.value.code == "bundle_signature_missing"
    assert _snapshot(tmp_path) == before
    assert authority.calls == []


def test_a_policy_for_a_home_reads_that_home_s_own_store(tmp_path: Path) -> None:
    home = tmp_path / "target-home"
    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)

    # Nothing enrolled yet: the same key is unknown on this target.
    with pytest.raises(SignatureKeyError):
        receive_bundle(
            bundle_dir, tmp_path / "landed", signature=trust_policy_for(home)
        )

    store_path = home / "secrets" / "transfer" / "trusted-source-keys.json"
    store_path.parent.mkdir(parents=True)
    os.chmod(home / "secrets", 0o700)
    os.chmod(store_path.parent, 0o700)
    store_path.write_text(json.dumps(_store(public).as_dict()))

    result = receive_bundle(
        bundle_dir, tmp_path / "landed", signature=trust_policy_for(home)
    )
    assert result.signature is not None
    assert result.signature.key_id == public_key_id(public)


def test_no_production_call_site_can_land_an_unsigned_bundle() -> None:
    """A tripwire over ``src/``: only tests may allow an unsigned landing."""

    root = Path(__file__).resolve().parents[4]
    offenders: list[str] = []
    policy_sites: set[str] = set()
    for path in sorted((root / "src" / "hyprial").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "allow_unsigned=True" in text or "UNSIGNED_REHEARSAL" in text:
            offenders.append(str(path.relative_to(root)))
        if "trust_policy_for(" in text and "def trust_policy_for" not in text:
            policy_sites.add(str(path.relative_to(root)))

    assert offenders == []
    assert policy_sites == {
        "src/hyprial/daemon/impl/application/actors/transfer.py",
        "src/hyprial/daemon/impl/application/actors/transfer_p2.py",
        "src/hyprial/shell/impl/cli/commands/transfer/bundle.py",
    }


# -- read binding: one manifest reading across validation, gate and copy -------


def test_a_manifest_swapped_after_validation_cannot_rebind_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one reading is the binding: a swap after validation is refused.

    The attack this pins: validation reads manifest A (payload P_A), and a
    local writer who can write the staging bundle then swaps in a *signed*
    manifest B before the gate.  A gate that re-opens the file weighs B's
    signature and names B's actor in the receipt while the copy loop still
    lands P_A -- landed bytes the signature never covered.  Under one reading
    the gate still weighs A, the swapped-in signature does not cover it, and
    the receive refuses with the target byte-identical.
    """

    carrier = _bundle(tmp_path / "carrier", actor="xfer-src2")
    donor = _bundle(tmp_path / "donor")  # the enrolled identity, signed
    private, public = generate_signing_key()
    _sign(donor, private_bytes=private)

    import hyprial.daemon.impl.transfer.landing.receive as receive_module

    real_validate = receive_module.validate_bundle
    swapped = {"done": False}

    def swapping_validate(bundle_dir, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_validate(bundle_dir, *args, **kwargs)
        shutil.copyfile(donor / "manifest.json", carrier / "manifest.json")
        shutil.copyfile(donor / SIGNATURE_NAME, carrier / SIGNATURE_NAME)
        swapped["done"] = True
        return result

    monkeypatch.setattr(receive_module, "validate_bundle", swapping_validate)
    destination = tmp_path / "target-home"

    with pytest.raises(SignatureInvalidError) as caught:
        receive_bundle(carrier, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_signature_invalid"
    assert swapped["done"]  # the swap really happened after validation
    # The refusal wrote nothing: no home, no staged leftovers.
    assert not destination.exists()
    assert _snapshot(destination) == {}


def test_a_payload_swapped_after_validation_is_refused_before_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy re-hashes every staged file against the bound manifest.

    The attack this pins: the bundle validates, and then a local writer
    rewrites a payload file before the copy reaches it.  A copy that trusts
    the earlier validation lands bytes no gate verified; with the read bound,
    the staged copy fails its manifest hash, the attempt is refused under the
    bundle's own tamper code, and everything created is undone.
    """

    bundle_dir = _bundle(tmp_path)
    private, public = generate_signing_key()
    _sign(bundle_dir, private_bytes=private)
    destination = tmp_path / "target-home"

    import hyprial.daemon.impl.transfer.landing.receive as receive_module

    real_copy = receive_module.shutil.copyfile
    tampered = {"done": False}

    def racing_copy(source, target, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not tampered["done"] and Path(source).name == "notes.md":
            # Same length, different bytes: only the digest can catch this.
            Path(source).write_text("evil bundle!\n")
            tampered["done"] = True
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(receive_module.shutil, "copyfile", racing_copy)

    with pytest.raises(BundleTamperError) as caught:
        receive_bundle(bundle_dir, destination, signature=_policy(_store(public)))

    assert caught.value.code == "bundle_tampered"
    assert "notes.md" in str(caught.value)
    assert tampered["done"]  # the rewrite really fired before the copy
    assert not destination.exists()
