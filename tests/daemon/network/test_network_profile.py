"""Network profile schema, resolution and idempotent writes (login U1, T1–T7+T9).

Every test here builds its **own** explicit home: the autouse fixture
(``tests/conftest.py``) exports ``HYPRIAL_OWNER`` and writes a ``settings.json``
into *its* fixture home, and the "no profile.json ⇒ built-in default"
assertions must not depend on that fixture happening not to write a
``profile.json`` (spec §2, the #352 lesson).

Since the tailnet cutover (2026-10-03) the record is schema v2: only ``org``,
``issuer`` and ``clientId``.  A v1 record still reads, its retired
``controlPlane``/``join`` fields ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hyprial.daemon.impl.configuration.network_profile as network_profile
from hyprial.daemon.impl.configuration.identity import _settings_path
from hyprial.daemon.impl.configuration.network_profile import (
    DEFAULT_PROFILE,
    NetworkProfile,
    profile_path,
    read_profile,
    resolve_profile,
    write_profile,
)


def _empty_home(tmp_path: Path) -> Path:
    """A home with nothing in it, passed explicitly — never the fixture's."""

    home = tmp_path / "own-home"
    home.mkdir(mode=0o700)
    return home


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "version": 2,
        "org": "acme",
        "issuer": "https://auth.acme.io",
        "clientId": "test-client-id",
    }
    record.update(overrides)
    return record


def _v1_record(**overrides: object) -> dict[str, object]:
    """A pre-cutover record: v1 with the retired control-plane fields."""

    record: dict[str, object] = {
        "version": 1,
        "org": "acme",
        "issuer": "https://auth.acme.io",
        "clientId": "test-client-id",
        "controlPlane": {"kind": "headscale", "url": "https://hs.acme.io"},
        "join": "interactive",
    }
    record.update(overrides)
    return record


def _write_record(home: Path, record: dict[str, object]) -> Path:
    path = home / "profile.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# -- T1: missing file = the built-in hyprial default --------------------------


def test_d1_missing_file_is_the_default_profile(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)

    resolved, source = resolve_profile({"HYPRIAL_HOME": str(home)}, hyprial_home=home)

    assert (resolved, source) == (DEFAULT_PROFILE, "built-in default")
    # The issuer is compared byte for byte: U2 requires discovery's issuer to
    # equal it verbatim, so the default's exact literal is contractual.
    assert resolved.issuer == "https://auth.hyprial.com"
    assert resolved.org == "hyprial"
    assert resolved.client_id == "2cdd1defc1a0e219fafb"


def test_t1_default_profile_object_matches_default_record(tmp_path: Path) -> None:
    """A file holding the default's exact values reads back equal to it."""

    home = _empty_home(tmp_path)
    write_profile(DEFAULT_PROFILE, hyprial_home=home)

    resolved, source = resolve_profile({"HYPRIAL_HOME": str(home)}, hyprial_home=home)

    assert source == "profile.json"
    assert resolved == NetworkProfile(
        org="hyprial",
        issuer="https://auth.hyprial.com",
        client_id="2cdd1defc1a0e219fafb",
    )
    assert resolved == DEFAULT_PROFILE


def test_t1_default_issuer_mutation_would_flip_the_assertion(tmp_path: Path) -> None:
    """The byte-for-byte comparison is load-bearing: any other issuer is a
    *different* profile, not the default."""

    home = _empty_home(tmp_path)
    other = NetworkProfile(
        org="hyprial",
        issuer="https://auth.hyprial.co",  # one character off the default
        client_id="2cdd1defc1a0e219fafb",
    )
    write_profile(other, hyprial_home=home)

    resolved, _source = resolve_profile({"HYPRIAL_HOME": str(home)}, hyprial_home=home)

    assert resolved != DEFAULT_PROFILE


# -- v1 records read with the retired fields ignored ---------------------------


def test_a_v1_record_reads_with_control_plane_and_join_ignored(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    path = _write_record(home, _v1_record())

    profile = read_profile(path)

    assert profile == NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
    )


def test_a_v2_record_rejects_the_retired_fields(tmp_path: Path) -> None:
    """Once a record declares v2, the retired keys are unknown keys."""

    home = _empty_home(tmp_path)
    path = _write_record(
        home,
        _record(controlPlane={"kind": "headscale", "url": "https://hs.acme.io"}),
    )

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert "unknown top-level key" in str(caught.value)


# -- T2: a broken file fails loudly, never falls back -------------------------


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("missing-issuer", {k: v for k, v in _record().items() if k != "issuer"}),
        ("issuer-trailing-slash", _record(issuer="https://auth.acme.io/")),
        ("issuer-http", _record(issuer="http://auth.acme.io")),
        ("issuer-query", _record(issuer="https://auth.acme.io?x=1")),
        ("issuer-fragment", _record(issuer="https://auth.acme.io#top")),
        ("unknown-top-key", _record(extra="oops")),
        ("missing-client-id", {k: v for k, v in _record().items() if k != "clientId"}),
        ("client-id-empty", _record(clientId="")),
        ("client-id-whitespace", _record(clientId="test client")),
        ("version-3", _record(version=3)),
        ("version-missing", {k: v for k, v in _record().items() if k != "version"}),
        ("org-empty", _record(org="")),
        ("org-colon", _record(org="a:b")),
        ("org-slash", _record(org="a/b")),
    ],
)
def test_t2_broken_records_raise_with_path(
    tmp_path: Path, label: str, record: dict[str, object]
) -> None:
    home = _empty_home(tmp_path)
    path = _write_record(home, record)

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert str(path) in str(caught.value), (
        f"{label}: the error must name the file it rejected: {caught.value}"
    )


def test_t2_unparseable_file_raises(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    path = home / "profile.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        resolve_profile({"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    assert str(path) in str(caught.value)


def test_t2_no_fallback_to_default_for_broken_file(tmp_path: Path) -> None:
    """The mutation probe for T2: `ValueError` must never become
    `(DEFAULT_PROFILE, "built-in default")` — "broken" and "absent" are
    different operator situations."""

    home = _empty_home(tmp_path)
    _write_record(home, _record(version=7))

    with pytest.raises(ValueError):
        resolve_profile({"HYPRIAL_HOME": str(home)}, hyprial_home=home)


# -- T3: exactly one issuer ---------------------------------------------------


def test_t3_issuer_as_array_is_rejected(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    path = _write_record(home, _record(issuer=["https://a.io", "https://b.io"]))

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert "issuer" in str(caught.value)


def test_t3_duplicate_issuer_keys_are_rejected(tmp_path: Path) -> None:
    """Two issuer keys in one object must not resolve to "the first" or "the
    last" — the record is rejected before either could win."""

    home = _empty_home(tmp_path)
    path = home / "profile.json"
    raw = json.dumps(_record())
    text = raw.replace(
        '"issuer": "https://auth.acme.io",',
        '"issuer": "https://auth.acme.io", "issuer": "https://evil.io",',
        1,
    )
    assert text.count('"issuer"') == 2

    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert "duplicate key" in str(caught.value)


# -- T5: profile_path and _settings_path share one home rule ------------------


@pytest.mark.parametrize("with_home_env", [True, False])
def test_t5_profile_path_shares_the_settings_home_rule(
    tmp_path: Path, with_home_env: bool
) -> None:
    env = {"HYPRIAL_HOME": str(tmp_path)} if with_home_env else {}

    assert profile_path(env).parent == _settings_path(env, None).parent
    assert profile_path(env).name == "profile.json"
    assert _settings_path(env, None).name == "settings.json"


def test_t5_explicit_hyprial_home_wins_for_both(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)

    assert (
        profile_path({}, home).parent
        == _settings_path({"HYPRIAL_HOME": str(tmp_path / "other")}, home).parent
        == home
    )


# -- T6: write_profile is idempotent and leaves settings.json alone -----------


def test_t6_write_twice_is_byte_identical(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    profile = NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
    )

    first = write_profile(profile, hyprial_home=home)
    bytes_after_first = first.read_bytes()
    second = write_profile(profile, hyprial_home=home)

    assert first == second == home / "profile.json"
    assert second.read_bytes() == bytes_after_first


def test_t6_write_does_not_touch_settings_json(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    settings = home / "settings.json"
    settings.write_text('{"owner": "someone", "updateTrack": "dev"}\n', encoding="utf-8")
    before = settings.read_bytes()

    write_profile(
        NetworkProfile(
            org="acme",
            issuer="https://auth.acme.io",
            client_id="test-client-id",
        ),
        hyprial_home=home,
    )

    assert settings.read_bytes() == before


def test_t6_invalid_profile_never_reaches_disk(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)

    with pytest.raises(ValueError):
        write_profile(
            NetworkProfile(
                org="acme",
                issuer="not-a-url",
                client_id="test-client-id",
            ),
            hyprial_home=home,
        )
    assert not (home / "profile.json").exists()
    assert list(home.iterdir()) == []


# -- T7 (schema half): create's differing-value refusal lives in the CLI tests;
# here the record-level comparison it relies on -------------------------------


def test_t7_equal_profiles_compare_equal_regardless_of_write(tmp_path: Path) -> None:
    """`hyprial profile create` refuses when the existing record differs; the
    comparison underneath is plain dataclass equality of validated records."""

    home = _empty_home(tmp_path)
    original = NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
    )
    write_profile(original, hyprial_home=home)

    reread = read_profile(home / "profile.json")

    assert reread == original

    differing = NetworkProfile(
        org="acme",
        issuer="https://auth.other.io",  # same name, different issuer
        client_id="test-client-id",
    )
    assert reread != differing


# -- inviteBaseUrl: the optional v2 invite landing-page host (§5.1) -----------


def test_invite_base_url_defaults_to_the_placeholder() -> None:
    """The placeholder stands until operations delivers INVITE_HOST (§5.1)."""

    assert DEFAULT_PROFILE.invite_base_url == "https://hyprial.ai"
    assert NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
    ).invite_base_url == "https://hyprial.ai"


def test_v2_record_without_invite_base_url_reads_the_default(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    path = _write_record(home, _record())

    profile = read_profile(path)

    assert profile.invite_base_url == "https://hyprial.ai"


def test_v2_invite_base_url_round_trips(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)
    profile = NetworkProfile(
        org="acme",
        issuer="https://auth.acme.io",
        client_id="test-client-id",
        invite_base_url="https://invites.acme.io",
    )

    path = write_profile(profile, hyprial_home=home)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["inviteBaseUrl"] == "https://invites.acme.io"
    assert read_profile(path) == profile


@pytest.mark.parametrize(
    "value",
    [
        "http://invites.acme.io",  # not https
        "https://invites.acme.io?x=1",  # query
        "https://invites.acme.io#top",  # fragment
        "https://",  # empty host
        42,  # not a string
    ],
)
def test_v2_invite_base_url_validation(tmp_path: Path, value: object) -> None:
    home = _empty_home(tmp_path)
    path = _write_record(home, _record(inviteBaseUrl=value))

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert "inviteBaseUrl" in str(caught.value)


def test_v1_record_rejects_invite_base_url_as_unknown(tmp_path: Path) -> None:
    """inviteBaseUrl is a v2 key; a v1 record never carried it."""

    home = _empty_home(tmp_path)
    path = _write_record(
        home, _v1_record(inviteBaseUrl="https://invites.acme.io")
    )

    with pytest.raises(ValueError) as caught:
        read_profile(path)
    assert "unknown top-level key" in str(caught.value)


def test_write_profile_rejects_a_bad_invite_base_url(tmp_path: Path) -> None:
    home = _empty_home(tmp_path)

    with pytest.raises(ValueError):
        write_profile(
            NetworkProfile(
                org="acme",
                issuer="https://auth.acme.io",
                client_id="test-client-id",
                invite_base_url="http://invites.acme.io",
            ),
            hyprial_home=home,
        )
    assert not (home / "profile.json").exists()


# -- T9: the issuer literal exists in exactly one place -----------------------


_SRC_ROOT = Path(__file__).resolve().parents[3] / 'src'
_ISSUER_LITERAL = "auth.hyprial.com"


def _literal_occurrences(literal: str) -> dict[Path, list[int]]:
    found: dict[Path, list[int]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        lines = [
            number
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            )
            if literal in line
        ]
        if lines:
            found[path] = lines
    return found


def test_t9_issuer_literal_only_in_default_profile() -> None:
    """Spec §2: the default issuer's domain may appear **only** in
    ``DEFAULT_PROFILE`` — U2+ code reads ``profile.issuer`` instead of
    re-typing the constant.  Mutation: write the literal anywhere else in
    ``src/`` and this goes red."""

    occurrences = _literal_occurrences(_ISSUER_LITERAL)

    assert set(occurrences) == {Path(network_profile.__file__)}, (
        f"{_ISSUER_LITERAL} must appear only in network_profile.py; found: "
        f"{ {str(path): lines for path, lines in occurrences.items()} }"
    )
    lines = occurrences[Path(network_profile.__file__)]
    assert len(lines) == 1, (
        f"expected exactly one occurrence (the DEFAULT_PROFILE issuer line), "
        f"found {lines}"
    )
    module_text = Path(network_profile.__file__).read_text(
        encoding="utf-8"
    )
    module_lines = module_text.splitlines()
    issuer_line = module_lines[lines[0] - 1]
    assert 'issuer="https://' in issuer_line, (
        f"the single occurrence must be the DEFAULT_PROFILE issuer assignment, "
        f"got: {issuer_line!r}"
    )
    context = "\n".join(module_lines[max(0, lines[0] - 8) : lines[0]])
    assert "DEFAULT_PROFILE = NetworkProfile(" in context, (
        "the issuer literal must live inside the DEFAULT_PROFILE assignment, "
        f"context:\n{context}"
    )


def test_t9_default_profile_docstring_anchor_exists() -> None:
    """The DEFAULT_PROFILE documentation segment asserting exclusivity must
    exist: the note and the tripwire point at each other, so neither can
    disappear alone."""

    doc = network_profile.__doc__ or ""
    assert "唯一允许出现" in doc, (
        "network_profile's module docstring must keep the DEFAULT_PROFILE "
        "segment stating the issuer literal's only allowed location"
    )
    assert "DEFAULT_PROFILE" in doc
    # The note must not *itself* re-type the literal it declares exclusive.
    assert _ISSUER_LITERAL not in doc
