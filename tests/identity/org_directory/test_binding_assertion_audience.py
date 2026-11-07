"""RFC 7519 ``aud`` shapes for binding assertions, and one judgement only.

Casdoor signs the binding access token with ``aud`` as a one-element array
(``["1be2763a29154030fbcb"]``), so every audience check in the tree goes
through :func:`binding_assertion_audience_matches`.  The cases below pin the
shapes that must be accepted and the shapes that must stay refused, at the
helper layer, independently of any token plumbing.

Decision (emergent, 2026-11-02T14:02Z): login and daemon use the same
judgement -- "登录侧与 daemon 侧用同一套判定 … 不搞两套".  The reason a second
judgement must not appear: a proof accepted at login and then refused by the
nodes is a silent failure, and that is the failure this hotfix removes.  So the
rule is enforced, not documented: the last test in this module fails when any
module outside ``identity/impl/org_directory/models.py`` reads the audience
claim directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hyprial.identity import (
    BINDING_ASSERTION_CLIENT_ID,
    binding_assertion_audience_matches,
)

OTHER = "another-client-id"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"aud": BINDING_ASSERTION_CLIENT_ID}, id="aud-string"),
        pytest.param({"aud": [BINDING_ASSERTION_CLIENT_ID]}, id="aud-array-casdoor-form"),
        pytest.param(
            {"aud": [BINDING_ASSERTION_CLIENT_ID, OTHER], "azp": BINDING_ASSERTION_CLIENT_ID},
            id="aud-multi-azp-is-us",
        ),
    ],
)
def test_accepts_audiences_that_name_this_application(claims) -> None:
    assert binding_assertion_audience_matches(claims, BINDING_ASSERTION_CLIENT_ID)


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({}, id="aud-absent"),
        pytest.param({"aud": OTHER}, id="aud-string-other-client"),
        pytest.param({"aud": [OTHER]}, id="aud-array-other-client"),
        pytest.param({"aud": []}, id="aud-empty-array"),
        pytest.param({"aud": [BINDING_ASSERTION_CLIENT_ID, OTHER]}, id="aud-multi-no-azp"),
        pytest.param(
            {"aud": [BINDING_ASSERTION_CLIENT_ID, OTHER], "azp": OTHER}, id="azp-other-client"
        ),
        pytest.param(
            {"aud": [BINDING_ASSERTION_CLIENT_ID, 7], "azp": BINDING_ASSERTION_CLIENT_ID},
            id="aud-non-string-member",
        ),
        pytest.param({"aud": 12345}, id="aud-wrong-type"),
        pytest.param({"aud": {"aud": BINDING_ASSERTION_CLIENT_ID}}, id="aud-nested-object"),
        pytest.param({"aud": None}, id="aud-null"),
    ],
)
def test_refuses_audiences_that_do_not_bind_this_application(claims) -> None:
    assert not binding_assertion_audience_matches(claims, BINDING_ASSERTION_CLIENT_ID)


_ALLOWED_AUDIENCE_READERS = {"src/hyprial/identity/impl/org_directory/models.py"}
_AUDIENCE_ACCESS = re.compile(r"\[\s*[\"']aud[\"']\s*\]|get\(\s*[\"']aud[\"']\s*[,)]")


def test_only_the_shared_helper_inspects_the_audience_claim() -> None:
    root = Path(__file__).resolve().parents[3]
    offenders = sorted(
        str(path.relative_to(root))
        for path in (root / "src").rglob("*.py")
        if str(path.relative_to(root)) not in _ALLOWED_AUDIENCE_READERS
        and _AUDIENCE_ACCESS.search(path.read_text(encoding="utf-8"))
    )

    assert offenders == [], (
        "the audience claim must be read through "
        "binding_assertion_audience_matches; these modules read it directly: "
        f"{offenders}"
    )
