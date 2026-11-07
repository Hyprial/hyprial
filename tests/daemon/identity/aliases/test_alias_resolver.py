"""P2 alias resolution, display, and disabled peer-source contracts."""

from __future__ import annotations

from collections.abc import Mapping

from hyprial.daemon.impl.alias_resolver import (
    AliasKind,
    AliasRecord,
    AliasResolver,
    AliasStatus,
    AliasSurface,
    PeopleDirectoryAliasSource,
    StaticAliasSource,
    VerifiedPeopleBinding,
)


def _source(*records: AliasRecord) -> StaticAliasSource:
    return StaticAliasSource(
        records,
        local={
            AliasKind.USER: AliasRecord(AliasKind.USER, "alice", "u.user-a"),
            AliasKind.DEVICE: AliasRecord(AliasKind.DEVICE, "hq", "d.device-a"),
        },
    )


def test_a_resolution_is_exact_and_never_fuzzy_prefix_or_casefold() -> None:
    resolver = AliasResolver(
        _source(
            AliasRecord(AliasKind.USER, "alice", "u.user-a"),
            AliasRecord(AliasKind.DEVICE, "hq", "d.device-a"),
            AliasRecord(AliasKind.AGENT, "Builder", "a.agent-a"),
        )
    )

    assert resolver.resolve(AliasKind.AGENT, "Builder", within_domain=True).identifier == "a.agent-a"
    for spelling in ("builder", "Build", "Builder-extra"):
        answer = resolver.resolve(AliasKind.AGENT, spelling, within_domain=True)
        assert answer.status is AliasStatus.UNKNOWN
        assert answer.identifier is None


def test_a_ambiguous_result_names_sorted_candidate_ids() -> None:
    resolver = AliasResolver(
        _source(
            AliasRecord(AliasKind.AGENT, "worker", "a.second"),
            AliasRecord(AliasKind.AGENT, "worker", "a.first"),
        )
    )

    answer = resolver.resolve(AliasKind.AGENT, "worker", within_domain=True)

    assert answer.status is AliasStatus.AMBIGUOUS
    assert answer.candidates == ("a.first", "a.second")


def test_freed_and_retaken_agent_name_resolves_only_the_current_id() -> None:
    rows = [AliasRecord(AliasKind.AGENT, "foo", "a.old")]
    source = StaticAliasSource(
        rows,
        local={
            AliasKind.USER: AliasRecord(AliasKind.USER, "alice", "u.user-a"),
            AliasKind.DEVICE: AliasRecord(AliasKind.DEVICE, "hq", "d.device-a"),
        },
    )
    resolver = AliasResolver(source)
    assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == "a.old"

    source.replace(AliasRecord(AliasKind.AGENT, "foo", "a.new"))

    assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == "a.new"
    assert resolver.resolve(AliasKind.AGENT, "a.old", within_domain=True).status is AliasStatus.UNKNOWN
    assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == "a.new"


def test_reused_people_alias_resolves_only_the_current_id() -> None:
    source = _source(AliasRecord(AliasKind.USER, "bob", "u.old"))
    resolver = AliasResolver(source)
    assert resolver.resolve(AliasKind.USER, "bob", within_domain=True).identifier == "u.old"

    source.replace(
        AliasRecord(AliasKind.USER, "alice", "u.user-a"),
        AliasRecord(AliasKind.DEVICE, "hq", "d.device-a"),
        AliasRecord(AliasKind.USER, "bob", "u.new"),
    )

    assert resolver.resolve(AliasKind.USER, "u.old", within_domain=True).status is AliasStatus.UNKNOWN
    assert resolver.resolve(AliasKind.USER, "bob", within_domain=True).identifier == "u.new"


def test_d_people_source_is_empty_without_rows_and_ignores_unverified_or_mismatched_rows() -> None:
    rows: list[Mapping[str, object]] = []

    def verify(row: Mapping[str, object]) -> VerifiedPeopleBinding | None:
        if row.get("proof") != "fixture-verified":
            return None
        return VerifiedPeopleBinding(
            username=str(row.get("proofName")),
            user_id=str(row.get("proofSub")),
        )

    people = PeopleDirectoryAliasSource(lambda: tuple(rows), verify=verify)
    resolver = AliasResolver(_source(), sources=(people,))
    assert resolver.resolve(AliasKind.USER, "bob", within_domain=False).status is AliasStatus.OUTSIDE_DOMAIN

    rows.extend(
        [
            {
                "user": "bob",
                "larkUnionId": "on_good",
                "proof": "fixture-verified",
                "proofName": "bob",
                "proofSub": "user-b",
                "publishedAt": "2026-10-07T00:00:00Z",
            },
            {
                "user": "mallory",
                "larkUnionId": "on_bad",
                "proof": "not-verified",
                "proofName": "mallory",
                "proofSub": "user-m",
                "publishedAt": "2026-10-07T00:00:00Z",
            },
            {
                "user": "eve",
                "larkUnionId": "on_mismatch",
                "proof": "fixture-verified",
                "proofName": "not-eve",
                "proofSub": "user-e",
                "publishedAt": "2026-10-07T00:00:00Z",
            },
        ]
    )

    assert resolver.resolve(AliasKind.USER, "bob", within_domain=False).identifier == "u.user-b"
    assert resolver.resolve(AliasKind.USER, "mallory", within_domain=False).status is AliasStatus.OUTSIDE_DOMAIN
    assert resolver.resolve(AliasKind.USER, "eve", within_domain=False).status is AliasStatus.OUTSIDE_DOMAIN


def test_a_outside_domain_is_unchanged_and_logged_once_per_alias() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    resolver = AliasResolver(
        _source(),
        log=lambda event, **fields: events.append((event, fields)),
    )
    target = "agent:other:peer:worker"

    assert resolver.validate_target(target, surface=AliasSurface.CLI) == target
    assert resolver.validate_target(target, surface=AliasSurface.CLI) == target

    assert events == [
        (
            "alias.unresolved",
            {
                "kind": "user",
                "surface": "cli",
            },
        )
    ]


def test_addendum_unresolved_event_name_and_fields_are_stable() -> None:
    assert {kind.value for kind in AliasKind} == {"user", "device", "agent"}
    assert {surface.value for surface in AliasSurface} == {
        "adapter",
        "cli",
        "daemon",
        "mcp",
        "pac",
        "routine",
    }
    events: list[tuple[str, dict[str, object]]] = []
    for surface in AliasSurface:
        resolver = AliasResolver(
            _source(),
            log=lambda event, **fields: events.append((event, fields)),
        )
        target = f"user:outside-{surface.value}"
        assert resolver.validate_target(target, surface=surface) == target

    assert events == [
        (
            "alias.unresolved",
            {
                "kind": "user",
                "surface": surface.value,
            },
        )
        for surface in AliasSurface
    ]


def test_round_4_unresolved_dedup_is_bounded_and_scoped_by_kind_and_surface() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    resolver = AliasResolver(
        _source(),
        log=lambda event, **fields: events.append((event, fields)),
    )

    # The same user text on different entry points must remain independently
    # countable, without putting that user text in telemetry.
    resolver.validate_target("user:outside", surface=AliasSurface.CLI)
    resolver.validate_target("user:outside", surface=AliasSurface.MCP)
    assert events == [
        ("alias.unresolved", {"kind": "user", "surface": "cli"}),
        ("alias.unresolved", {"kind": "user", "surface": "mcp"}),
    ]

    # Fill and overflow the fixed-size LRU. The first key is evicted and can
    # be counted again; no attacker-controlled set can grow without bound.
    for index in range(1025):
        resolver.validate_target(
            f"user:outside-{index}", surface=AliasSurface.ROUTINE
        )
    resolver.validate_target("user:outside-0", surface=AliasSurface.ROUTINE)
    assert len(events) == 1028
    assert all(set(fields) == {"kind", "surface"} for _, fields in events)


def test_round_4_render_is_deferred_until_a_production_display_uses_it() -> None:
    assert not hasattr(AliasResolver, "render")
