"""Contract examples for membership reads alongside the current org catalog."""
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

MEMBERSHIP_EXAMPLES = (
    MethodExampleSet(
        method="org.invites.list",
        empty_not_applicable="The success case already represents an empty invitation list.",
        cases=(
            MethodExample(
                name="no-pending-invites", kind="success", params={"invites": []},
                result={"ok": True, "pending": [], "settled": 0, "duplicates": 0, "warnings": []},
                description="The account has no invitations requiring a decision.",
            ),
            MethodExample(
                name="invites-required", kind="invalid-params", params={},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "invites is required"),
                description="The caller must supply its account invitation list.",
            ),
            MethodExample(
                name="service-not-ready", kind="domain-error", params={"invites": []},
                error=MethodExampleError(ipc_errors.DAEMON_NOT_READY, "organization membership is not ready"),
                description="The membership service has not been composed.",
            ),
        ),
    ),
)
