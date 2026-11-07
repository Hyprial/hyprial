"""Invite link encoding, decoding, and every named failure (§4.1)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from hyprial.identity.impl.org_directory.bootstrap import (
    bootstrap_yaml,
    default_bootstrap,
    parse_bootstrap_yaml,
)
from hyprial.identity.impl.org_directory.invite import (
    INVITE_DIGEST_MISMATCH,
    INVITE_EXPIRED,
    INVITE_MALFORMED,
    INVITE_SCHEME,
    InviteError,
    InviteLink,
    decode_invite,
    encode_invite,
)


def _workflow_yaml() -> str:
    return bootstrap_yaml(default_bootstrap())


def _link(**overrides: str) -> InviteLink:
    fields: dict[str, str] = {
        "org": "group-acme",
        "space_id": "space-1",
        "acl_space_id": "space-acl-1",
        "inviter": "alicesmith@example.org",
        "inviter_device": "node-7f3a",
        "inviter_address": "10.0.0.1:41271#psk-secret",
        "token": secrets.token_urlsafe(24),
        "workflow_yaml": _workflow_yaml(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    fields.update(overrides)
    if "workflow_sha256" not in overrides:
        fields["workflow_sha256"] = hashlib.sha256(
            fields["workflow_yaml"].encode("utf-8")
        ).hexdigest()
    fields['tailnet'] = {
        'version': 2, 'tailnetId': fields['space_id'], 'inviteId': 'invite-1',
        'token': 't' * 43, 'owner': 'invitee@example.org',
        'expiresAtMs': int(datetime.fromisoformat(fields['expires_at'].replace('Z', '+00:00')).timestamp() * 1000),
        'authority': {'owner': fields['inviter'], 'deviceId': fields['inviter_device'],
                      'masterPublic': 'nodekey:' + 'a' * 64, 'serverPublic': 'nodekey:' + 'b' * 64,
                      'address': fields['inviter_address'], 'controlPort': 7448},
    }
    return InviteLink(**fields)


def _expired_link(**overrides: str) -> InviteLink:
    return _link(
        expires_at="2020-01-01T00:00:00Z",
        **overrides,
    )


# -- the happy path --------------------------------------------------------------


def test_encode_decode_round_trips_every_field() -> None:
    link = _link()
    assert decode_invite(encode_invite(link)) == link


def test_encoded_form_is_scheme_plus_unpadded_base64url() -> None:
    link = _link()
    text = encode_invite(link)
    assert text.startswith(INVITE_SCHEME)
    body = text[len(INVITE_SCHEME) :]
    assert "=" not in body
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    payload = json.loads(decoded.decode("utf-8"))
    assert set(payload) == {
        "org",
        "spaceId",
        "aclSpaceId",
        "inviter",
        "inviterDevice",
        "inviterAddress",
        "token",
        "workflowYaml",
        "workflowSha256",
        "expiresAt",
        "tailnet",
    }
    assert payload["org"] == "group-acme"
    assert payload["inviterDevice"] == "node-7f3a"


def test_encoding_is_deterministic() -> None:
    link = _link()
    assert encode_invite(link) == encode_invite(link)


def test_the_carried_workflow_parses() -> None:
    link = _link()
    restored = decode_invite(encode_invite(link))
    assert restored.workflow_yaml == link.workflow_yaml
    assert parse_bootstrap_yaml(restored.workflow_yaml) == default_bootstrap()


# -- expiry ------------------------------------------------------------------------


def test_an_expired_link_is_refused() -> None:
    with pytest.raises(InviteError) as caught:
        decode_invite(encode_invite(_expired_link()))
    assert caught.value.code == INVITE_EXPIRED


def test_expiry_at_exactly_now_is_refused() -> None:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with pytest.raises(InviteError) as caught:
        decode_invite(encode_invite(_link(expires_at=now)))
    assert caught.value.code == INVITE_EXPIRED


#: The landing page's own check (hyprial.ai/join app.js): RFC 3339 with ``Z``.
_LANDING_EXPIRES = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")


def _wire_payload(encoded: str) -> dict[str, object]:
    body = encoded[len(INVITE_SCHEME) :]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


@pytest.mark.parametrize(
    "expires",
    ["2030-01-01T00:00:00+00:00", "2030-01-01T00:00:00.123456+00:00", "2030-01-01T00:00:00Z"],
)
def test_the_wire_expiry_always_ends_in_z_for_the_landing_page(expires: str) -> None:
    payload = _wire_payload(encode_invite(_link(expires_at=expires)))

    assert _LANDING_EXPIRES.fullmatch(str(payload["expiresAt"]))
    assert decode_invite(encode_invite(_link(expires_at=expires))) is not None


def test_a_link_valid_tomorrow_still_decodes() -> None:
    expires = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )
    assert decode_invite(encode_invite(_link(expires_at=expires))) is not None


# -- digest ------------------------------------------------------------------------


def _tampered_payload_link(tamper) -> str:
    link = _link()
    body = encode_invite(link)[len(INVITE_SCHEME) :]
    payload = json.loads(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    )
    tamper(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return INVITE_SCHEME + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_a_tampered_workflow_yaml_breaks_the_digest() -> None:
    text = _tampered_payload_link(
        lambda payload: payload.update(
            {
                "workflowYaml": bootstrap_yaml(
                    default_bootstrap()[:1]
                )
            }
        )
    )
    with pytest.raises(InviteError) as caught:
        decode_invite(text)
    assert caught.value.code == INVITE_DIGEST_MISMATCH


def test_a_forged_digest_is_refused() -> None:
    link = _link(workflow_sha256="0" * 64)
    with pytest.raises(InviteError) as caught:
        decode_invite(encode_invite(link))
    assert caught.value.code == INVITE_DIGEST_MISMATCH


def test_digest_is_checked_before_expiry() -> None:
    # An expired link with a broken digest reports the digest: the workflow
    # bytes are what the invitee would execute, so they are verified first.
    link = _expired_link(workflow_sha256="0" * 64)
    with pytest.raises(InviteError) as caught:
        decode_invite(encode_invite(link))
    assert caught.value.code == INVITE_DIGEST_MISMATCH


# -- malformed ---------------------------------------------------------------------


def test_a_missing_scheme_is_refused() -> None:
    link = _link()
    body = encode_invite(link)[len(INVITE_SCHEME) :]
    for text in ("", body, f"https://invite/{body}"): 
        with pytest.raises(InviteError) as caught:
            decode_invite(text)
        assert caught.value.code == INVITE_MALFORMED


def test_garbage_after_the_scheme_is_refused() -> None:
    for text in (
        f"{INVITE_SCHEME}!!!not-base64!!!",
        f"{INVITE_SCHEME}" + base64.urlsafe_b64encode(b"\x00\xff").decode().rstrip("="),
    ):
        with pytest.raises(InviteError) as caught:
            decode_invite(text)
        assert caught.value.code == INVITE_MALFORMED


def test_valid_base64_that_is_not_json_is_refused() -> None:
    raw = base64.urlsafe_b64encode(b"plain text").rstrip(b"=").decode("ascii")
    with pytest.raises(InviteError) as caught:
        decode_invite(f"{INVITE_SCHEME}{raw}")
    assert caught.value.code == INVITE_MALFORMED


def test_a_payload_with_missing_or_bad_fields_is_refused() -> None:
    link = _link()
    body = encode_invite(link)[len(INVITE_SCHEME) :]
    payload = json.loads(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    )

    def encode(payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return INVITE_SCHEME + base64.urlsafe_b64encode(raw).rstrip(b"=").decode(
            "ascii"
        )

    bad_payloads = [
        {**payload, "inviterAddress": ""},
        {**payload, "org": "Not-A-Org"},
        {**payload, "token": "has spaces and :/"},
        {**payload, "workflowSha256": "nothex"},
        {**payload, "expiresAt": "tomorrow"},
        {key: value for key, value in payload.items() if key != "spaceId"},
        {key: value for key, value in payload.items() if key != "aclSpaceId"},
        {**payload, "extra": "field"},
    ]
    for bad in bad_payloads:
        with pytest.raises(InviteError) as caught:
            decode_invite(encode(bad))
        assert caught.value.code == INVITE_MALFORMED


def test_legacy_pending_payload_requires_reissue_even_when_wrapped_as_v2():
    body = encode_invite(_link())[len(INVITE_SCHEME):]
    payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    del payload['tailnet']
    old = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
    with pytest.raises(InviteError) as caught:
        decode_invite('v2.' + old)
    assert caught.value.code == 'INVITE_VERSION_UNSUPPORTED'


def test_encoding_refuses_shape_broken_links() -> None:
    with pytest.raises(InviteError) as caught:
        encode_invite(_link(org="Not-A-Org"))
    assert caught.value.code == INVITE_MALFORMED
    with pytest.raises(InviteError) as caught:
        encode_invite(_link(workflow_sha256="zz"))
    assert caught.value.code == INVITE_MALFORMED


def test_invite_error_is_a_value_error_with_a_code() -> None:
    error = InviteError(INVITE_MALFORMED, "boom")
    assert isinstance(error, ValueError)
    assert error.code == INVITE_MALFORMED


# -- the three §5.1 forms: one payload, three transports ------------------------


def _payload_of(link: InviteLink) -> str:
    return encode_invite(link)[len(INVITE_SCHEME) :]


def test_https_form_round_trips_and_matches_the_payload() -> None:
    from hyprial.identity.impl.org_directory.invite import invite_https_url

    link = _link()
    url = invite_https_url(link, "https://invite.example.org")
    assert url == f"https://invite.example.org/join#v2.{_payload_of(link)}"
    assert decode_invite(url) == link


def test_https_form_tolerates_a_trailing_slash_in_the_base_url() -> None:
    from hyprial.identity.impl.org_directory.invite import invite_https_url

    link = _link()
    url = invite_https_url(link, "https://invite.example.org/")
    assert url.startswith("https://invite.example.org/join#v2.")
    assert decode_invite(url) == link


def test_https_form_refuses_a_non_https_base_url() -> None:
    from hyprial.identity.impl.org_directory.invite import invite_https_url

    with pytest.raises(InviteError) as caught:
        invite_https_url(_link(), "http://invite.example.org")
    assert caught.value.code == INVITE_MALFORMED


def test_deep_link_form_round_trips() -> None:
    from hyprial.identity.impl.org_directory.invite import invite_deep_link

    link = _link()
    deep = invite_deep_link(link)
    assert deep == f"hyprial://join#v2.{_payload_of(link)}"
    assert decode_invite(deep) == link


def test_bare_v2_payload_decodes() -> None:
    link = _link()
    assert decode_invite(f"v2.{_payload_of(link)}") == link


def test_all_four_forms_of_one_link_decode_equal() -> None:
    from hyprial.identity.impl.org_directory.invite import (
        invite_deep_link,
        invite_https_url,
    )

    link = _link()
    forms = [
        encode_invite(link),
        invite_https_url(link, "https://invite.example.org"),
        invite_deep_link(link),
        f"v2.{_payload_of(link)}",
    ]
    assert [decode_invite(form) for form in forms] == [link] * 4


def test_surrounding_whitespace_and_paired_quotes_are_stripped() -> None:
    link = _link()
    text = encode_invite(link)
    assert decode_invite(f"  {text}\n") == link
    assert decode_invite(f"'{text}'") == link
    assert decode_invite(f'\n"{text}" \t') == link
    quoted_url = f"'https://invite.example.org/join#v2.{_payload_of(link)}'"
    assert decode_invite(f" {quoted_url} ") == link


def test_https_form_accepts_any_host_and_longer_join_paths() -> None:
    # LAX(tailnet-cutover): no host whitelist this round; the path rule is
    # "ends with /join".
    link = _link()
    payload = _payload_of(link)
    assert decode_invite(f"https://evil.example/join#v2.{payload}") == link
    assert decode_invite(f"https://a.example/some/sub/join#v2.{payload}") == link


@pytest.mark.parametrize(
    "text",
    [
        "https://invite.example.org/join",  # no fragment at all
        "https://invite.example.org/join#payload-without-marker",
        "https://invite.example.org/#v2.abc",  # path does not end with /join
        "https://invite.example.org/joinx#v2.abc",
        "https://invite.example.org#v2.abc",  # no path
        "http://invite.example.org/join#v2.abc",  # not https
        "hyprial://other#v2.abc",  # wrong deep-link authority
        "hyprial://join",  # no fragment
        "hyprial://join#v3.abc",  # wrong fragment version
        "v3.abc",  # bare payload of another version
    ],
)
def test_malformed_url_forms_are_refused(text: str) -> None:
    with pytest.raises(InviteError) as caught:
        decode_invite(text)
    assert caught.value.code == INVITE_MALFORMED
