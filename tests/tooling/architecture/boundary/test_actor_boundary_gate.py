"""Actor-runtime static boundaries and controlled-mutation proofs."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.gates.actor_boundary_gate import scan_actor_boundaries, validate_legacy_manifest

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"
MANIFEST = Path(__file__).resolve().parents[4] / 'tests/actor_boundary_exemptions.json'


def _write_module(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _rules(root: Path) -> set[str]:
    return {violation.rule for violation in scan_actor_boundaries(root)}


def test_repository_actor_boundaries_are_closed() -> None:
    violations = scan_actor_boundaries(SRC)
    assert violations == [], "actor boundary violations:\n" + "\n".join(
        violation.render() for violation in violations
    )


def test_legacy_big_bang_exemptions_are_exact_and_shrinking() -> None:
    errors = validate_legacy_manifest(SRC, MANIFEST)
    assert errors == [], "legacy exemption manifest drift:\n" + "\n".join(errors)


def test_mutation_rejects_pykka_import_outside_the_backend(tmp_path: Path) -> None:
    _write_module(tmp_path, "domain/messages.py", "import pykka\n")
    assert "pykka-import" in _rules(tmp_path)


def test_mutation_allows_pykka_import_only_in_the_backend(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "kernel/impl/actor_runtime/backends/pykka.py",
        "import pykka\n_private_ref: pykka.ActorRef[object]\n",
    )
    assert scan_actor_boundaries(tmp_path) == []


def test_mutation_rejects_every_raw_domain_type(tmp_path: Path) -> None:
    capabilities = ("ActorRef", "ActorProxy", "Future", "ThreadingActor", "ActorInbox")
    for index, capability in enumerate(capabilities):
        _write_module(
            tmp_path,
            f"domain/handler_{index}.py",
            "from hyprial.kernel.impl.actor_runtime.backends.pykka import " f"{capability}\n",
        )
    violations = scan_actor_boundaries(tmp_path)
    for capability in capabilities:
        assert any(capability in violation.detail for violation in violations)


def test_mutation_rejects_raw_pykka_proxy_access(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "domain/proxy_handler.py",
        "def dispatch(ref: object) -> None:\n    ref.proxy()\n",
    )
    violations = scan_actor_boundaries(tmp_path)
    assert any(".proxy()" in violation.detail for violation in violations)


def test_mutation_rejects_raw_pykka_mailbox_access(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "domain/mailbox_handler.py",
        "def dispatch(ref: object) -> None:\n    ref._actor_inbox.put({})\n",
    )
    violations = scan_actor_boundaries(tmp_path)
    assert any("mailbox attribute" in violation.detail for violation in violations)


def test_mutation_rejects_public_annotation_leaks(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "kernel/impl/actor_runtime/backends/pykka.py",
        "from pykka import ActorRef as RawRef\n"
        "\n"
        "def leaked(ref: RawRef[object]) -> RawRef[object]:\n"
        "    return ref\n",
    )
    assert "public-api-leak" in _rules(tmp_path)


def test_mutation_rejects_all_exports_of_pykka_types(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "kernel/impl/actor_runtime/backends/pykka.py",
        "from pykka import ActorProxy as RawProxy\n\n__all__ = [\"RawProxy\"]\n",
    )
    violations = scan_actor_boundaries(tmp_path)
    assert any("__all__" in violation.detail for violation in violations)


def test_mutation_rejects_blocking_ask_but_not_explicit_nonblocking_ask(
    tmp_path: Path,
) -> None:
    _write_module(
        tmp_path,
        "domain/handler.py",
        "def blocked(ref: object) -> object:\n"
        "    return ref.ask({\"command\": \"run\"})\n"
        "\n"
        "def nonblocking(ref: object) -> object:\n"
        "    return ref.ask({\"command\": \"run\"}, block=False)\n",
    )
    violations = scan_actor_boundaries(tmp_path)
    blocking = [item for item in violations if item.rule == "blocking-ask"]
    assert len(blocking) == 1
    assert blocking[0].line == 2


def test_mutation_rejects_new_unmanifested_legacy_symbol(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "daemon/impl/inbox/service/__init__.py",
        "class InboxService:\n    pass\n\nclass ShadowService:\n    pass\n",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": "shrinking-only",
                "exemptions": [
                    {
                        "path": "src/hyprial/daemon/impl/inbox/service/__init__.py",
                        "symbol": "InboxService",
                        "reason": "pre-cutover owner",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    errors = validate_legacy_manifest(tmp_path, manifest)
    assert errors == [
        "unmanifested legacy cutover symbol: "
        "src/hyprial/daemon/impl/inbox/service/__init__.py:ShadowService"
    ]


def test_mutation_rejects_stale_legacy_exemption_after_cutover(
    tmp_path: Path,
) -> None:
    _write_module(tmp_path, "daemon/impl/inbox/service/__init__.py", "class ActorInbox:\n    pass\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": "shrinking-only",
                "exemptions": [
                    {
                        "path": "src/hyprial/daemon/impl/inbox/service/__init__.py",
                        "symbol": "InboxService",
                        "reason": "pre-cutover owner",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    errors = validate_legacy_manifest(tmp_path, manifest)
    assert errors == [
        "stale legacy exemption must shrink now: "
        "src/hyprial/daemon/impl/inbox/service/__init__.py:InboxService"
    ]


def test_duplicate_native_owner_cannot_hide_behind_one_legacy_record(tmp_path: Path) -> None:
    source = 'class InboxService:\n    pass\n\nclass InboxService:\n    pass\n'
    _write_module(tmp_path, 'daemon/impl/inbox/service/__init__.py', source)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'policy': 'shrinking-only',
        'exemptions': [{'path': 'src/hyprial/daemon/impl/inbox/service/__init__.py',
                        'symbol': 'InboxService', 'reason': 'fixture'}]}))
    assert any('non-unique legacy authority definition' in error
               for error in validate_legacy_manifest(tmp_path, manifest))
