from hyprial.daemon.impl.lifecycle.health.write_rate import WriteRateWindow


def test_only_sustained_excess_pages_once_and_recovery_is_observable():
    window = WriteRateWindow(bytes_per_second=50, sustain_seconds=300)
    assert window.observe(0, 0) is None
    assert window.observe(60 * 100, 100) is None
    assert window.observe(60 * 300, 300) is None
    assert window.observe(60 * 301, 301) == "high"
    assert window.observe(60 * 400, 400) is None
    assert window.observe(60 * 400, 401) == "recovered"
    assert window.status()["alarming"] is False


def test_counter_reset_and_one_spike_cannot_page():
    window = WriteRateWindow(bytes_per_second=50, sustain_seconds=300)
    window.observe(0, 0)
    window.observe(6000, 100)
    window.observe(6000, 200)
    assert window.observe(12000, 400) is None
    assert window.observe(0, 500) is None
    assert window.status()["aboveSince"] is None


def test_real_custody_writer_counters_feed_health_and_increase(tmp_path):
    import sqlite3
    from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessAuthority
    from hyprial.daemon.impl.lifecycle.health.database import DatabaseHealth

    connection = sqlite3.connect(tmp_path / "inbox.sqlite3")
    connection.execute("CREATE TABLE marker(value TEXT)")
    connection.close()
    writer = OrphanProcessAuthority(tmp_path / "orphans.json", identity_reader=lambda _: None)
    process = object()

    def diagnostics():
        stats = writer.persistence_stats()
        return {"orphanWriteBytes": stats["appendBytes"] + stats["snapshotBytes"]}

    health = DatabaseHealth(tmp_path, epoch="integration", databases=("inbox.sqlite3",),
                            log=lambda *a, **k: None, diagnostics=diagnostics)
    try:
        writer.observe_start("fixture:worker", process, pid=None, marker=None)
        before = health.check()
        assert before["writeRate"]["available"] is True
        assert before["runtime"]["orphanWriteBytes"] > 0
        writer.observe_stop("fixture:worker", process, pid=None, marker=None)
        after = health.check()
        assert after["runtime"]["orphanWriteBytes"] > before["runtime"]["orphanWriteBytes"]
    finally:
        assert writer.close()
