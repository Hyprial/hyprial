"""Composition over daemon-owned live ports; never opens a second authority."""
from pathlib import Path

from hyprial.daemon.impl.org.network.ipc import _invalid_device_diagnostics

from .apply import MigrationService
from .inventory import snapshot
from .journal import new_report, write_report


def run(app) -> dict:
    try:
        fs = app._require_orgfs_runtime().facade
    except Exception as error:
        report = new_report(str(app.node_id))
        report["failed"].append({"entity": "runtime", "reason": getattr(error, "code", type(error).__name__)})
        write_report(Path(app.hyprial_home), report)
    else:
        report = MigrationService(fs=fs, home=Path(app.hyprial_home), owner=str(app.owner),
                                  node_id=str(app.node_id), inventory=lambda: snapshot(app, fs),
                                  invalid_devices=_invalid_device_diagnostics(app), log=app._log).run()
    app._log("info", "org", "org.migration.report", operationId=report["operationId"],
             node=report["node"], verified=report["verified"],
             failed=len(report["failed"]), pending=len(report["pending"]))
    return report
