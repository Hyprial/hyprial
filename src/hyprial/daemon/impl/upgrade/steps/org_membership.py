"""Repeatable migration through the live daemon port, never a second replica."""
from . import UpgradeContext, UpgradeStep


def _run(context: UpgradeContext) -> dict:
    if context.membership_migration is None:
        return {"schemaVersion": 1, "failed": [],
                "pending": [{"entity": "runtime", "reason": "daemon-membership-unavailable"}]}
    report = context.membership_migration()
    if report.get("verified") is not True or report.get("conflicts"):
        if not report.get("failed") and not report.get("pending") and not report.get("unconfirmed"):
            report = {**report, "pending": [{"entity": "readback", "reason": "migration-unverified"}]}
    return report


STEP = UpgradeStep("org-membership-v1", _run, repeatable=True)
