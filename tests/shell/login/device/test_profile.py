"""login: profile scenarios."""

from __future__ import annotations


def test_t2_origin_comparison_rules() -> None:
    """The origin unit under test: scheme+host+effective port — nothing
    looser; path-insensitivity is correct (endpoints live under paths)."""

    from hyprial.shell.impl.login.oidc import _origin

    assert _origin("https://a.io") == _origin("https://a.io:443")
    assert _origin("https://a.io/x/y") == _origin("https://a.io")
    assert _origin("https://a.io") != _origin("https://a.io:8443")
    assert _origin("http://a.io") != _origin("https://a.io")
    assert _origin("https://a.io") != _origin("https://b.io")
