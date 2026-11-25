"""Real snapshot and registered text inspection with a deterministic sampler."""

from hyprial.daemon.impl.operations import top
from tests.support.golden.app import golden_daemon as golden_daemon


def test_top_inspection_golden(golden_daemon, monkeypatch):
    daemon = golden_daemon()
    monkeypatch.setattr(top, "_sample_process_table", lambda: {})
    golden = daemon.golden("convergence", "top-inspect")
    result = golden.call("top.inspect", {})
    assert all(row["processMetrics"] is None for row in result["actors"])
    golden.check()
