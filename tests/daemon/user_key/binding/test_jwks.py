"""Injected discovery/JWKS documents; no network or account credentials."""
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hyprial.daemon.impl.identity.jwks import JwksSource, JwksUnavailable


def number(value):
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).decode().rstrip("=")


@pytest.fixture
def source():
    public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    numbers = public.public_numbers()
    issuer = "https://synthetic.invalid"
    discovery = {"issuer": issuer, "jwks_uri": issuer + "/keys"}
    document = {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "test",
                          "e": number(numbers.e), "n": number(numbers.n)}]}
    now, calls = [0.0], []
    def request(url):
        calls.append(url)
        return discovery if url.endswith("openid-configuration") else document
    return JwksSource(issuer=issuer, request_json=request, monotonic=lambda: now[0]), discovery, document, now, calls


def test_cache_expires_and_has_no_stale_key_fallback(source):
    reader, _, _, clock, calls = source
    first = reader()
    assert len(calls) == 2
    assert reader()["test"].public_numbers() == first["test"].public_numbers()
    assert len(calls) == 2
    def unavailable(url):
        raise OSError("SENSITIVE-SENTINEL")
    reader.request_json = unavailable
    clock[0] = 300.0
    with pytest.raises(JwksUnavailable) as caught:
        reader()
    assert "SENSITIVE-SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("field,value", [("issuer", "https://other.invalid"),
    ("jwks_uri", "https://other.invalid/keys"), ("jwks_uri", "http://synthetic.invalid/keys"),
    ("jwks_uri", "https://user@synthetic.invalid/keys"), ("jwks_uri", "https://synthetic.invalid/keys#fragment")])
def test_discovery_exact_issuer_and_same_origin(source, field, value):
    reader, discovery, _, _, calls = source
    discovery[field] = value
    with pytest.raises(JwksUnavailable):
        reader()
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("use", "enc"), ("key_ops", ["sign"]),
    ("alg", "RS512"), ("n", "bad=="), ("e", ""), ("kid", "")])
def test_jwks_invalid_key_is_unavailable(source, field, value):
    reader, _, document, _, _ = source
    document["keys"][0][field] = value
    with pytest.raises(JwksUnavailable):
        reader()


def test_duplicate_kid_is_not_last_writer_wins(source):
    reader, _, document, _, _ = source
    document["keys"].append(dict(document["keys"][0]))
    with pytest.raises(JwksUnavailable):
        reader()


def test_issuer_rotation_takes_effect_at_ttl(source):
    reader, _, document, clock, _ = source
    assert "test" in reader()
    document["keys"][0]["kid"] = "rotated"
    assert "test" in reader()
    clock[0] = 300.0
    assert set(reader()) == {"rotated"}
