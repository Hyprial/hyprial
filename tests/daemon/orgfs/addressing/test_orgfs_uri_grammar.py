"""orgfs:<owner>:<spaceId>:<nodeId> grammar — pure, binding (design §1/§2/§7a).

One writer (``canonical_orgfs_uri``), one reader (``parse_orgfs_uri``); the
web URL is derived per tailnet and never a constant.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.kernel.impl.primitives.uri import (
    OrgfsWebNetwork,
    canonical_orgfs_uri,
    delivery_address_error,
    orgfs_web_host,
    orgfs_web_url,
    parse_orgfs_uri,
    uri_scheme)

OWNER = "bob.smith@example.com"
SPACE_ID = "6b69777a-3e0e-4d2b-9d8c-3f6a7b1c2d4e"
NODE_ID = "9bf0e3a1c4d94b6f8e2a7c5d0f1b3a4c"

#: The two worked examples of design §1.
DOC_URI = f"orgfs:{OWNER}:{SPACE_ID}:{NODE_ID}"
ROOT_URI = f"orgfs:{OWNER}:{SPACE_ID}:root"


def test_canonical_orgfs_uri_round_trips_the_worked_examples() -> None:
    assert canonical_orgfs_uri(OWNER, SPACE_ID, NODE_ID) == DOC_URI
    assert parse_orgfs_uri(DOC_URI) == (OWNER, SPACE_ID, NODE_ID)
    assert canonical_orgfs_uri(OWNER, SPACE_ID, "root") == ROOT_URI
    assert parse_orgfs_uri(ROOT_URI) == (OWNER, SPACE_ID, "root")


@pytest.mark.parametrize(
    "value",
    [
        # 3 segments — kill-mutation 1: a parser that accepts these turns
        # this test red.
        "orgfs:bob.smith@example.com:6b69777a-3e0e-4d2b-9d8c-3f6a7b1c2d4e",
        # 5 segments.
        "orgfs:a:b:c:d",
        # A user:-prefixed owner splits into five segments → None (the free
        # kill-switch for a producer that forgets to strip, design §1).
        f"orgfs:user:{OWNER}:{SPACE_ID}:{NODE_ID}",
        # Empty segments.
        "orgfs::b:c",
        "orgfs:a::c",
        "orgfs:a:b:",
        "orgfs:::",
        # Whitespace-padded segments (2026-09-14 S4 rule).
        "orgfs: a:b:c",
        "orgfs:a: b:c",
        "orgfs:a:b:c ",
        # The `/` ban (HTTP path mapping stays bijective).
        "orgfs:a/b:c:d",
        "orgfs:a:b:c/d",
        # Not an orgfs URI at all.
        "agent:bob.smith@example.com:machine:actor",
        "user:bob.smith@example.com",
        "orgfs:",
        "",
    ],
)
def test_parse_orgfs_uri_rejects_non_canonical_shapes(value: str) -> None:
    assert parse_orgfs_uri(value) is None


@pytest.mark.parametrize(
    "owner, space_id, node_id",
    [
        ("", SPACE_ID, NODE_ID),
        (OWNER, "", NODE_ID),
        (OWNER, SPACE_ID, ""),
        (" a", SPACE_ID, NODE_ID),
        (OWNER, SPACE_ID, "x "),
        (f"user:{OWNER}", SPACE_ID, NODE_ID),  # ':' in a segment
        (OWNER, "space/id", NODE_ID),  # '/' in a segment
        (OWNER, SPACE_ID, "node/id"),
    ],
)
def test_canonical_orgfs_uri_rejects_impure_segments(
    owner: str, space_id: str, node_id: str
) -> None:
    with pytest.raises(ValueError):
        canonical_orgfs_uri(owner, space_id, node_id)


def test_orgfs_uri_classification_stays_untouched() -> None:
    # The scheme classifier already returns "orgfs"; the identity classifier
    # labels it unknown (a label, never a rejection); and an orgfs URI is an
    # address of a document, never a message target.
    assert uri_scheme(DOC_URI) == "orgfs"
    assert classify_target_identity(DOC_URI) == "unknown"
    assert delivery_address_error(DOC_URI) is not None


def test_orgfs_web_host_prefers_override_then_suffix_then_fqdn() -> None:
    assert (
        orgfs_web_host(OrgfsWebNetwork(web_host="files.corp-b.example"))
        == "files.corp-b.example"
    )
    # MagicDNSSuffix is authoritative and beats the FQDN (no label-stripping).
    assert (
        orgfs_web_host(
            OrgfsWebNetwork(
                magic_dns_suffix="tail1234.ts.net",
                fqdn="node.other-domain.example",
            )
        )
        == "fs.tail1234.ts.net"
    )
    # FQDN minus its first label, trailing dot tolerated.
    assert (
        orgfs_web_host(OrgfsWebNetwork(fqdn="node-hq-01-drill.corp-b.example."))
        == "fs.corp-b.example"
    )
    # A single-label name has no domain to derive from; nothing is guessed.
    assert orgfs_web_host(OrgfsWebNetwork(fqdn="localhost")) is None
    assert orgfs_web_host(OrgfsWebNetwork()) is None


def test_orgfs_web_url_is_the_three_segments_verbatim() -> None:
    assert (
        orgfs_web_url(DOC_URI, network=OrgfsWebNetwork(fqdn="node.corp-b.example"))
        == f"https://fs.corp-b.example/{OWNER}/{SPACE_ID}/{NODE_ID}"
    )
    # No host derivable → the URL is absent, the URI stands alone.
    assert orgfs_web_url(DOC_URI, network=OrgfsWebNetwork()) is None
    with pytest.raises(ValueError):
        orgfs_web_url("orgfs:not-a-uri", network=OrgfsWebNetwork())
