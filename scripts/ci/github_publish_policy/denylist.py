"""The public-snapshot denylist: what must not leave the intranet.

Decision record: Allen 2026-11-03 -- publish everything except this table
(docs/design/public-snapshot-denylist-2026-11-03.md).  Each key is a
repository-relative path (a trailing ``/`` names a directory) and each value
is the reviewable reason.  ``scripts/ci/github_publish.py`` re-exports it as
``SNAPSHOT_DENYLIST``; nothing else may restate these paths.
"""

from __future__ import annotations

SNAPSHOT_DENYLIST: dict[str, str] = {
    "crates/hyprial-py/tests/": "native binding Python helpers are not cargo build inputs",
    ".github/": "intranet CI: runners, hosts and secret names; never runs on GitHub",
    ".idea/": "local IDE configuration",
    ".agents/skills/forgejo-ops/": "internal Forgejo operations runbook",
    "docs/evidence/": "reviewer-owned raw evidence (WORM); intranet hosts, people, client ids",
    "docs/notes/": "evidence, repro material and handoffs (WORM); intranet hosts and people",
    "gui/evidence/": "GUI acceptance evidence (WORM)",
    "gui/docs/": "GUI docs and acceptance records (Allen 2026-09-16, re-anchored 2026-11-03)",
    "gui/vendor/": "reserved: vendored GUI code (Allen 2026-09-16, re-anchored 2026-11-03); empty today",
    "gui/tests/": "GUI tests with tailnet-shaped fixtures (Allen 2026-09-16, re-anchored 2026-11-03)",
    "edge/": "Cloudflare deployment configuration; not reviewed for publication",
    "scripts/archived/": "retired scripts that still name intranet hosts",
    "tests/fixtures/agent_home_p20/tool-profile/": "pinned P20 probe fixture with the internal remote",
    "tests/tooling/verification/test_probe_agent_home_p20.py": "drives scripts/archived/ probe",
    "scripts/ci/": "intranet CI tooling: internal server defaults; drives .github/ only",
    "tests/tooling/ci/": "tests of intranet CI; read .github/, which is not published",
    "tests/tooling/release/": "tests of the intranet release pipeline; read .github/",
    "contract/e2e/": "real-environment E2E against internal hosts and credential refs",
    "tests/integration/e2e/": "tests that load contract/e2e (real test Apps and hosts)",
    "docs/design/gui-reference-slice/": "verbatim prompts naming people; pinned by SHA-256",
    "docs/design/service-connect/": "real service, HQ and DERP addresses (HQ re-review round 2)",
    "docs/design/actor-rustify-external-developer-guide-2026-11-01.md": "internal contributor handoff: original private repository and account provisioning instructions",
    "docs/design/actor-rustify-native-engine-handoff-2026-11-01.md": "internal engineering handoff: real agent contacts and tailnet routing addresses",
    "docs/design/daemon-request/c03-methodspec-2026-11-02.json": "method spec evidence contains real owner identifiers and private markers",
    "docs/design/daemon-request/c04-methodspec-2026-11-02.json": "method spec evidence contains real owner identifiers and private markers",
    "docs/design/daemon-request/methodspec-coverage-2026-11-02.json": "method coverage evidence contains real owner and device identifiers",
    "docs/design/org-membership-model-2026-10-29.md": "organization design example contains real owner identifiers",
    "docs/design/zenoh-acl-0553-goals/": "internal coordination documents contain real hosts and owner identifiers",
    "docs/design/zenoh-acl-1785-1850-takeover-plan-2026-10-25.md": "internal takeover plan contains real host and owner identifiers",
    "tests/daemon/contract_mock/test_preserved_prompts.py": "pins docs/design/gui-reference-slice/",
    "tests/daemon/network/test_network_isolated.py": "loads contract/e2e test-App lists",
    "tests/daemon/adapters/lark/fixtures/test_fake_lark_service.py": "loads contract/e2e fake",
    "contract/sidecar-tailcat/golden/": "nodekey-shaped protocol fixtures (gitleaks)",
    "scripts/refactor/preview/": "generated refactor preview artefacts (gitleaks)",
    "tests/fixtures/registry-bd6eb960/": "database dump fixture, not reviewed for publication",
    "tests/daemon/adapters/lark/sdk/test_lark_media.py": "synthetic media keys (gitleaks)",
    "tests/daemon/cells/observability/recovery/test_run_owners.py": "synthetic lease token (gitleaks)",
    "tests/daemon/delivery/identity/test_sender_identity.py": "synthetic user key (gitleaks)",
    "tests/daemon/routing/test_route_outbound.py": "credential reference fixture (gitleaks)",
    "tests/identity/agents/secret_storage/test_secret_store.py": "synthetic tokens (gitleaks)",
    "tests/daemon/orgfs/storage/store/frontiers/evidence.json": "recorded base64 CRDT frontier tokens trip generic-api-key (gitleaks); evidence only, no test reads it",
}
