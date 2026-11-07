"""Public identity error vocabulary for PAC expansion."""

import hyprial.identity as identity


ERROR_CODES = {
    "PAC_EXPANSION_ALREADY_BOUND",
    "PAC_EXPANSION_INVALID",
    "PAC_EXPANSION_POLICY_UNAVAILABLE",
    "PAC_EXPANSION_SYSTEM_NODE",
    "PAC_WORKTREE_CLEANUP_FAILED",
    "PAC_WORKTREE_IDENTITY_MISMATCH",
    "PAC_WORKTREE_PREPARE_FAILED",
}


def test_pac_expansion_errors_are_exact_public_identity_literals() -> None:
    for name in ERROR_CODES:
        assert getattr(identity, name) == name
        assert name in identity.__all__


def test_pac_expansion_invalid_preserves_structured_error_data() -> None:
    data = {"field": "nodes[0].kind", "reason": "kind"}
    error = identity.PacError(identity.PAC_EXPANSION_INVALID, "invalid expansion", data)

    assert error.code == "PAC_EXPANSION_INVALID"
    assert error.data == data
