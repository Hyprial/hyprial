"""A fresh connection, not an old open handle, proves restart readability."""
import json
import sqlite3
import threading
import subprocess
import sys

import pytest

from hyprial.daemon.impl.lifecycle.health.database import DatabaseHealth


def test_healthy_database_checked_without_replacing_it(tmp_path):
    path = tmp_path / "pac-graph.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE marker(value TEXT)")
        db.execute("INSERT INTO marker VALUES ('retained')")
    health = DatabaseHealth(tmp_path, epoch="test", databases=(path.name,), log=lambda *a, **k: None)
    assert health.check()["healthy"] is True
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "retained"
    assert not list(tmp_path.glob("*probe*"))


def test_missing_database_fails_loud_and_does_not_create_empty_replacement(tmp_path, capsys):
    events = []
    health = DatabaseHealth(tmp_path, epoch="test", databases=("missing.sqlite3",),
                            log=lambda *a, **k: events.append((a, k)))
    report = health.check()
    assert report["healthy"] is False
    assert report["failures"][0]["database"] == "missing.sqlite3"
    assert report["failures"][0]["sqliteErrorName"] == "SQLITE_CANTOPEN"
    assert "daemon.storage.unavailable" in capsys.readouterr().err
    assert events[0][0][1] == "daemon.storage.unavailable"
    assert not (tmp_path / "missing.sqlite3").exists()
    with pytest.raises(RuntimeError, match="database health"):
        health.require_healthy()


def test_late_failure_and_recovery_update_independent_heartbeat(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE marker(value TEXT)")
    events = []
    health = DatabaseHealth(tmp_path, epoch="test", databases=(path.name,),
                            log=lambda *a, **k: events.append(a))
    assert health.check()["healthy"]
    path.rename(tmp_path / "saved.sqlite3")
    assert not health.check()["healthy"]
    report = json.loads((tmp_path / "daemon-health.json").read_text())
    assert report["epoch"] == "test" and report["healthy"] is False
    (tmp_path / "saved.sqlite3").rename(path)
    assert health.check()["healthy"]
    assert events[-1][1] == "daemon.storage.recovered"


def test_heartbeat_survives_broken_main_logger_and_thread_closes(tmp_path, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("logger unavailable")
    health = DatabaseHealth(tmp_path, epoch="test", databases=("missing.sqlite3",), log=broken)
    health.check()
    assert json.loads((tmp_path / "daemon-health.json").read_text())["healthy"] is False
    assert "daemon.storage.unavailable" in capsys.readouterr().err
    stop = threading.Event()
    health.start(stop, interval=0.01)
    assert health.close(1)


def test_corrupt_database_is_fail_loud_without_repairing_it(tmp_path, capsys):
    path = tmp_path / "pac-graph.sqlite3"
    original = b"not a SQLite database" * 512
    path.write_bytes(original)
    health = DatabaseHealth(tmp_path, epoch="fixture", databases=(path.name,),
                            log=lambda *a, **k: None)
    report = health.check()
    assert report["healthy"] is False
    assert report["failures"][0]["sqliteErrorName"] == "SQLITE_NOTADB"
    assert path.read_bytes() == original
    assert "daemon.storage.unavailable" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor exhaustion fixture")
def test_fd_exhaustion_reports_sqlite_code_instead_of_claiming_wal_corruption(tmp_path):
    result = subprocess.run([sys.executable, "-c", '''
import json, os, resource, sqlite3, sys
from pathlib import Path
from hyprial.daemon.impl.lifecycle.health.database import DatabaseHealth
root=Path(sys.argv[1])
db=sqlite3.connect(root/'inbox.sqlite3');db.execute('CREATE TABLE marker(x)');db.close()
health=DatabaseHealth(root,epoch='fixture',databases=('inbox.sqlite3',),log=lambda *a,**k:None)
limits=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(64,limits[1]))
handles=[]
try:
 while True:
  try: handles.append(os.open('/dev/null',os.O_RDONLY))
  except OSError: break
 report=health.check()
finally:
 for fd in handles: os.close(fd)
 resource.setrlimit(resource.RLIMIT_NOFILE,limits)
print(json.dumps(report))
''', str(tmp_path)], capture_output=True, text=True, timeout=10, check=True)
    report = json.loads(result.stdout)
    assert report["healthy"] is False
    assert report["failures"][0]["sqliteErrorName"].startswith("SQLITE_CANTOPEN")
    assert "daemon.storage.unavailable" in result.stderr
