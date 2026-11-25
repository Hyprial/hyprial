"""Work-org conflict and empty report verification tests."""
from tests.daemon.org_membership.migration.test_migration import implementation


def test_step_refuses_unverified_empty_report(tmp_path):
    implementation()
    from hyprial.daemon.impl.upgrade import run_upgrade_steps
    result = run_upgrade_steps(tmp_path / "state", only=("org-membership-v1",), home=tmp_path,
        membership_migration=lambda: {"verified": False, "failed": [], "pending": []})
    assert result["steps"][0]["state"] == "pending"


