#!/usr/bin/env python3
"""AT01: real CLI -> isolated daemon -> old registry upgrade/restart.

No worker, model, container, or production write. Run with the checkout's
.venv/bin/python; --production-socket is used ONLY for before/after ping.
Evidence and synthetic state are retained in the printed temporary directory.
The daemon's own user agent (UA1b) is split out of the post-start registry
snapshot and checked on its own (``_user_agent.py``).  The invite steps this
scenario once ran were deleted with the host-invite feature (2026-11-21);
the harness, the fixture upgrade assertions and the shared base module stay
for the rest of the hosted family.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
_UA = importlib.util.spec_from_file_location("e2e_user_agent", Path(__file__).with_name("_user_agent.py"))
user_agent = importlib.util.module_from_spec(_UA)
_UA.loader.exec_module(user_agent)
TABLES = ("agents", "agent_secret_grants", "lifecycle_resources")


def paired_user_id(owner: str) -> str:
    """The isolated cell authority uid paired with a scenario owner."""

    return "ou_" + hashlib.sha256(f"user:{owner}".encode("utf-8")).hexdigest()[:32]


# Keep each exact layout tuple's registry last: AT02 appends that layout's grants
# module by replacing the bounded literal, not by adding an import fallback.
# Import timing brackets importlib.import_module only; the source check (resolve,
# read_bytes, sha256) and the evidence writes are timed separately or not at all.
#
# PROBE (AT04 card 6, criteria-v3.3 §3) runs first, in the tree's own interpreter, and
# uses only that interpreter's stdlib. When AT01_PROBE_EVIDENCE is set it times every
# importlib FileLoader.get_data and PathDistribution.read_text call (wall and thread
# CPU), and a daemon thread appends getrusage samples every 0.5 s, each line written
# and fsynced so a SIGKILLed start still leaves its record. Completed reads ride on the
# next sample; a read still running is listed as in flight. Strictly best effort: any
# probe failure leaves the start, the scenario verdict and readiness untouched.
PROBE = r'''
def _at01_probe():
    import os
    path = os.environ.get('AT01_PROBE_EVIDENCE')
    if not path:
        return None
    import atexit, collections, json, resource, sys, threading, time
    from datetime import datetime, timezone
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    lock = threading.Lock()
    fields = ('utime', 'stime', 'minflt', 'majflt', 'inblock', 'nvcsw', 'nivcsw')

    def emit(record):
        try:
            data = (json.dumps(record, separators=(',', ':')) + '\n').encode()
            with lock:
                os.write(fd, data)
                os.fsync(fd)
        except Exception:
            pass

    def stamp(**fields_):
        fields_.update(mono=time.monotonic(), utc=datetime.now(timezone.utc).isoformat())
        return fields_

    def usage():
        try:
            r = resource.getrusage(resource.RUSAGE_SELF)
            return {name: getattr(r, 'ru_' + name) for name in fields}
        except Exception:
            return None

    source = os.environ.get('AT01_SOURCE', '')
    prefixes = tuple((name, os.path.realpath(prefix) if prefix else None)
                     for name, prefix in
                     (('venv', sys.prefix),
                      ('src', os.path.join(source, 'src') if source else None),
                      ('stdlib', sys.base_prefix)))

    def category(target):
        text = os.path.realpath(os.fspath(target))
        for name, prefix in prefixes:
            if prefix and text.startswith(prefix.rstrip(os.sep) + os.sep):
                return name
        return 'other'

    done = collections.deque()
    inflight = {}
    calls = {'get_data': 0, 'read_text': 0}

    def timed(original, hook, target_of):
        def wrapper(self, *args, **kwargs):
            try:
                target = target_of(self, *args)
                kind = category(target)
                key = threading.get_ident()
                start, cpu = time.monotonic(), time.thread_time()
                inflight[key] = (start, kind, hook)
            except Exception:
                return original(self, *args, **kwargs)
            size = 0
            try:
                result = original(self, *args, **kwargs)
                try:
                    size = len(result) if result is not None else 0
                except Exception:
                    pass
                return result
            finally:
                try:
                    end = time.monotonic()
                    inflight.pop(key, None)
                    calls[hook] += 1
                    done.append([start, end, max(time.thread_time() - cpu, 0.0), kind, size,
                                 os.fspath(target) if end - start >= 0.05 else None, hook])
                except Exception:
                    pass
        wrapper.__at01_probe__ = True
        return wrapper

    hooks = {'get_data': False, 'read_text': False}
    try:
        import importlib._bootstrap_external as external
        loader = external.FileLoader
        loader.get_data = timed(loader.get_data, 'get_data', lambda self, target, *rest: target)
        hooks['get_data'] = True
    except Exception:
        pass
    try:
        import importlib.metadata as metadata
        distribution = metadata.PathDistribution
        distribution.read_text = timed(distribution.read_text, 'read_text',
                                       lambda self, filename, *rest: os.path.join(os.fspath(self._path), filename))
        hooks['read_text'] = True
    except Exception:
        pass

    try:
        load = list(os.getloadavg())
    except Exception:
        load = None
    installed = time.monotonic()
    emit(stamp(kind='install', pid=os.getpid(), loadavg=load, usage=usage(), hooks=hooks,
               python=sys.version.split()[0]))
    cap = float(os.environ.get('AT01_PROBE_MAX_SECONDS', '90'))
    stopped = threading.Event()
    first = [True]

    def sample(final=False):
        reads = []
        while True:
            try:
                reads.append(done.popleft())
            except IndexError:
                break
        record = stamp(kind='sample', usage=usage(), reads=reads,
                       inflight=[list(value) for value in list(inflight.values())], hookCalls=dict(calls))
        if first[0]:
            # criteria §3: the first P2 sample carries loadavg, the §6 pairing source
            first[0] = False
            try:
                record['loadavg'] = list(os.getloadavg())
            except Exception:
                record['loadavg'] = None
        if final:
            record['final'] = True
        emit(record)

    def run():
        while not stopped.wait(0.5):
            sample()
            if time.monotonic() - installed >= cap:
                break

    def finish():
        stopped.set()
        sample(final=True)

    threading.Thread(target=run, name='at01-probe', daemon=True).start()
    atexit.register(finish)

    def mark(name):
        emit(stamp(kind='mark', name=name))
    return mark

try:
    _at01_mark = _at01_probe()
except Exception:
    _at01_mark = None

def _at01_note(name):
    if _at01_mark is not None:
        try:
            _at01_mark(name)
        except Exception:
            pass
'''
BOOTSTRAP = PROBE + """
import faulthandler, hashlib, importlib, json, os, signal, time
from pathlib import Path
root = Path(os.environ['AT01_SOURCE'])
stack_dump = open(os.environ['AT01_FAULTHANDLER_EVIDENCE'], 'w')
faulthandler.register(signal.SIGUSR2, file=stack_dump, all_threads=True)
loaded = {}
timing = {}
source_check = 0.0
native_layout = (root / 'src/hyprial/shell/impl/cli/runtime/entry.py').is_file()
names = (('shell.impl.cli.runtime.entry', 'daemon.impl.application', 'identity.impl.agents.registry')
         if native_layout else ('cli', 'daemon.application', 'daemon.owner_migration', 'agents.registry'))
for name in names:
    checked = time.perf_counter()
    provider = root / 'src/hyprial' / name.replace('.', '/')
    candidates = (provider.with_suffix('.py'), provider / '__init__.py')
    providers = [candidate for candidate in candidates if candidate.is_file()]
    assert len(providers) == 1, (name, [str(candidate) for candidate in providers])
    expected_source = providers[0]
    assert not expected_source.is_symlink(), str(expected_source)
    assert expected_source.resolve().is_relative_to((root / 'src').resolve()), str(expected_source)
    source_check += time.perf_counter() - checked
    wall, cpu = time.perf_counter(), time.process_time()
    module = importlib.import_module('hyprial.' + name)
    timing[name] = {'wall': time.perf_counter() - wall, 'cpu': time.process_time() - cpu}
    checked = time.perf_counter()
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(root / 'src'), str(path)
    assert path == expected_source.resolve(), (name, str(path), str(expected_source))
    loaded[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    source_check += time.perf_counter() - checked
Path(os.environ['AT01_IMPORT_EVIDENCE']).write_text(json.dumps(loaded, indent=2))
if os.environ.get('AT01_TIMING_EVIDENCE'):
    core = names[:4]
    try:
        Path(os.environ['AT01_TIMING_EVIDENCE']).write_text(json.dumps({
            'modules': timing,
            'coreWall': sum(timing[n]['wall'] for n in core), 'coreCpu': sum(timing[n]['cpu'] for n in core),
            'allWall': sum(v['wall'] for v in timing.values()), 'allCpu': sum(v['cpu'] for v in timing.values()),
            'sourceCheckWall': source_check,
        }, indent=2))
    except Exception as error:  # timing is evidence only; the daemon must still start
        try:
            import sys
            print(f'AT01 timing evidence not written: {type(error).__name__}: {error}', file=sys.stderr)
        except Exception:
            pass  # reporting the failure is best effort too
_at01_note('imports-done')
import hyprial.cli
_at01_note('cli-main')
hyprial.cli.main()
"""


CORE_MODULES = ("shell.impl.cli.runtime.entry", "daemon.impl.application", "identity.impl.agents.registry")
LEGACY_CORE_MODULES = ("cli", "daemon.application", "daemon.owner_migration", "agents.registry")
TIMING_TOLERANCE = 1e-6
CLI_BUDGET = 30
# Replaceable in tests (virtual time for deadline and diagnostics cases).
clock = time.monotonic
sleep = time.sleep
#: Every Scenario root created in this process; main() lists their ledgers in result.json
#: so the suite knows which attempt ledgers must exist (nested worker-journey included).
SCENARIO_ROOTS = []


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def is_duration(value):
    """A finite, non-negative, float-representable number (bool and 10**400 are not)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def ledger_paths(root):
    """Attempt ledgers of every Scenario created under root, relative to root."""
    root = Path(root).resolve()
    found = []
    for scenario_root in SCENARIO_ROOTS:
        resolved = Path(scenario_root).resolve()
        if resolved == root or resolved.is_relative_to(root):
            found.append(str((resolved / "attempts.json").relative_to(root)))
    return sorted(set(found))


def read_import_timing(path, imports_path=None):
    """Never raises: any failure to read, parse or check the timing is a missing measurement."""
    try:
        return _read_import_timing(path, imports_path)
    except Exception as error:  # noqa: BLE001 - evidence only
        return {"coreWall": None, "coreCpu": None, "allWall": None, "allCpu": None, "modules": None,
                "note": f"timing unreadable: {type(error).__name__}"}


def _read_import_timing(path, imports_path=None):
    """Import totals from BOOTSTRAP's timing file, checked, never zero for missing data.

    coreWall/coreCpu must equal the sum over the core modules, allWall/allCpu the
    sum over every timed module, and (when imports.json is given) the timed modules
    must be exactly the imported ones. Anything else is null with a note."""
    empty = {"coreWall": None, "coreCpu": None, "allWall": None, "allCpu": None, "modules": None}
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return dict(empty, note="timing missing: not recorded (cause undetermined)")
    except (OSError, ValueError) as error:
        return dict(empty, note=f"timing unreadable: {type(error).__name__}")
    modules = data.get("modules") if isinstance(data, dict) else None
    if not isinstance(modules, dict) or not all(
            isinstance(entry, dict) and is_duration(entry.get("wall")) and is_duration(entry.get("cpu"))
            for entry in modules.values()):
        return dict(empty, note="timing malformed: modules")
    cores = [core for core in (CORE_MODULES, LEGACY_CORE_MODULES) if set(core) <= set(modules)]
    if len(cores) != 1:
        return dict(empty, note="timing malformed: a core module is missing")
    core_modules = cores[0]
    if imports_path is not None:
        try:
            imported = json.loads(Path(imports_path).read_text())
        except (OSError, ValueError) as error:
            return dict(empty, note=f"imports evidence unreadable: {type(error).__name__}")
        if not isinstance(imported, dict) or set(imported) != set(modules):
            return dict(empty, note="timing modules differ from imports.json")
    if not is_duration(data.get("sourceCheckWall")):
        return dict(empty, note="timing malformed: sourceCheckWall")
    totals = {"coreWall": sum(modules[n]["wall"] for n in core_modules),
              "coreCpu": sum(modules[n]["cpu"] for n in core_modules),
              "allWall": sum(entry["wall"] for entry in modules.values()),
              "allCpu": sum(entry["cpu"] for entry in modules.values())}
    for key, expected in totals.items():
        reported = data.get(key)
        if not is_duration(reported) or abs(reported - expected) > TIMING_TOLERANCE:
            return dict(empty, note=f"timing malformed: {key} is not the module sum")
    return dict(totals, modules=len(modules), note=None)


def _safe_timing(timing_path, imports_path):
    """read_import_timing, guarded again at the call site: evidence never replaces the outcome."""
    try:
        return read_import_timing(timing_path, imports_path)
    except Exception as error:  # noqa: BLE001
        return {"coreWall": None, "coreCpu": None, "allWall": None, "allCpu": None, "modules": None,
                "note": f"timing unreadable: {type(error).__name__}"}


def _text(value):
    if value is None or isinstance(value, str):
        return value
    return value.decode("utf-8", "replace")


def ping(path):
    return user_agent.request(path, "ping", timeout=3)


def production_identity(path):
    result = ping(path)
    return {key: result[key] for key in ("pid", "epoch")}


def request(path, method, params, *, ok=True):
    import socket as _socket

    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as client:
        client.settimeout(45)
        client.connect(str(path))
        client.sendall(json.dumps({"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            part = client.recv(65536)
            assert part, "closed before response"
            data += part
        result = json.loads(data.split(b"\n")[0])
        assert ("error" not in result) == ok, f"{method}: {result}"
        return result["result"] if ok else result["error"]


#: Nullable columns a later migration adds to an old registry.  They are left
#: out of the row comparison and checked instead as "no old row gained a value
#: on startup": absent before (count 0) and still all NULL after.  The
#: agent_secret_grants pair is the agent-shared owner (8135feacd, 2026-10-28),
#: NULL on every row whose source is not agent-shared.
ADDITIVE_COLUMNS = {
    "agents": ("last_active_at_ms", "display_name"),
    "agent_secret_grants": ("owner_actor", "owner_entity_token"),
}
#: UUID addressing §2.1/§2.4: the id each row derives from its own entity_token.
#: Left out of the row comparison; the snapshot counts the rows NOT carrying
#: exactly the derived id (None while the column is absent), so "P4 stamped
#: every row" is the expectation 0, never an ignored column.
DERIVED_ID_COLUMNS = {"agents": "agent_id", "agent_secret_grants": "agent_id", "retired_session_refs": "agent_id"}
#: Tables a newer registry creates on open, snapshotted whenever present, with
#: the columns they are created with (the derived column left out).
OPTIONAL_TABLES = {
    "retired_session_refs": ["actor", "entity_token", "session_ref", "retired_at_ms", "reason", "destroy_attempt"],
}


def agent_id_for(entity_token):
    """§2.1 derived id: ``a.`` + base32(sha256(tag + token)[:16]); None if the token derives none."""
    try:
        raw = bytes.fromhex(entity_token)
    except (TypeError, ValueError):
        return None
    if len(raw) != 16:
        return None
    digest = hashlib.sha256(b"hyprial-agent-id-v1" + raw).digest()[:16]
    return "a." + base64.b32encode(digest).decode().rstrip("=").lower()


def snapshot(root):
    rows, columns, added_non_null, derived = {}, {}, {}, {}
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in (*TABLES, *(name for name in OPTIONAL_TABLES if name in present)):
            names = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            added = [name for name in ADDITIVE_COLUMNS.get(table, ()) if name in names]
            stamped = DERIVED_ID_COLUMNS.get(table)
            columns[table] = [name for name in names if name not in added and name != stamped]
            kept = ", ".join(f'"{name}"' for name in columns[table])
            rows[table] = sorted(db.execute(f'SELECT {kept} FROM "{table}"'))
            added_non_null[table] = sum(
                db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{name}" IS NOT NULL').fetchone()[0]
                for name in added
            )
            if stamped:
                derived[table] = sum(value != agent_id_for(token) for token, value in db.execute(
                    f'SELECT entity_token, "{stamped}" FROM "{table}"')) if stamped in names else None
    files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "home/agents").rglob("*") if path.is_file()
    }
    return {"columns": columns, "rows": rows, "addedNonNull": added_non_null, "derivedIds": derived, "files": files}


def after_p4(old):
    """The snapshot the UUID P4 open must leave behind from the pre-P4 snapshot ``old``.

    Exactly the documented rewrite (docs/design/uuid-addressing-implementation-
    2026-11-03.md §2.4; registry/naming id_form.py and ensure_naming_schema);
    every other column, row and home file stays byte-identical.
    - agents: a name-form uri becomes agent:<its owner>:<its node>:<derived id>
      (a hosted row keeps its pinned owner); agent_id is stamped, display_name NULL.
    - agent_secret_grants, retired_session_refs: agent_id stamped; a live
      incarnation's retired refs follow its uri. Legacy grants gain exactly
      route_required=0; all existing grant columns remain unchanged.
    - lifecycle_resources: an agent-record payload gains agentId (from its own
      entityToken) and displayName null; its uri moves only when the payload
      still equalled the row (Agent.to_json), so a drifted fence keeps its drift.
      binding:<old uri> is re-keyed to the new uri, payload actor included.
    No used name is recorded anywhere (owner decision 2026-11-19).
    A snapshot already in id form maps to itself (P4 is idempotent).
    """
    assert not any(old["addedNonNull"].values()), old["addedNonNull"]
    new = json.loads(json.dumps({key: old[key] for key in ("columns", "addedNonNull", "files")}))
    new["rows"] = {table: [list(row) for row in table_rows] for table, table_rows in old["rows"].items()}
    if "route_required" not in new["columns"]["agent_secret_grants"]:
        new["columns"]["agent_secret_grants"].append("route_required")
        new["rows"]["agent_secret_grants"] = [row + [0] for row in new["rows"]["agent_secret_grants"]]
    for table, names in OPTIONAL_TABLES.items():
        new["columns"].setdefault(table, names)
        new["rows"].setdefault(table, [])
        new["addedNonNull"].setdefault(table, 0)
    new["derivedIds"] = dict.fromkeys(DERIVED_ID_COLUMNS, 0)
    at = {table: {name: i for i, name in enumerate(names)} for table, names in new["columns"].items()}
    records = {row[0]: row for row in new["rows"]["lifecycle_resources"]}
    for key, row in records.items():
        payload = json.loads(row[3]) if key.startswith("agent-record:") else None
        if isinstance(payload, dict) and "agentId" not in payload and agent_id_for(payload.get("entityToken")):
            payload.update(agentId=agent_id_for(payload["entityToken"]), displayName=payload.get("displayName"))
            row[3] = json.dumps(payload, sort_keys=True)
    agents, refs = at["agents"], at["retired_session_refs"]
    for agent in new["rows"]["agents"]:
        old_uri, token = agent[agents["uri"]], agent[agents["entity_token"]]
        owner, node, name = old_uri.removeprefix("agent:").split(":")
        if name.startswith("a."):
            continue
        agent[agents["uri"]] = uri = f"agent:{owner}:{node}:{agent_id_for(token)}"
        record = records.get(f"agent-record:{agent[agents['actor']]}")
        if record is not None and json.loads(record[3]) == record_json(new, agent, old_uri):
            record[3] = json.dumps(dict(json.loads(record[3]), uri=uri), sort_keys=True)
        binding = records.get(f"binding:{old_uri}")
        if binding is not None:
            payload = json.loads(binding[3])
            if isinstance(payload, dict) and payload.get("actor") == old_uri:
                payload["actor"] = uri
            binding[0], binding[3] = f"binding:{uri}", json.dumps(payload, sort_keys=True)
        for ref in new["rows"]["retired_session_refs"]:
            if ref[refs["actor"]] == old_uri and ref[refs["entity_token"]] == token:
                ref[refs["actor"]] = uri
    new["rows"] = {table: sorted(tuple(row) for row in table_rows) for table, table_rows in new["rows"].items()}
    return new


def record_json(snap, agent, uri):
    """Agent.to_json of one pre-P4 agents row (additive columns NULL), as P4's fence check reads it."""
    row = dict(zip(snap["columns"]["agents"], agent))
    def loads(text):
        return None if text is None else json.loads(text)
    return {
        "uri": uri, "actor": row["actor"], "owner": row["owner"], "machine": row["machine"],
        "entityToken": row["entity_token"], "cwd": row["cwd"], "config": loads(row["config"]),
        "provider": row["provider"], "model": row["model"], "capabilities": loads(row["capabilities"]),
        "harnessArgs": loads(row["harness_args"]), "preferredHarness": row["preferred_harness"],
        "lastHarness": row["last_harness"], "lastSessionId": row["last_session_id"], "lastActiveAtMs": None,
        "createdAtMs": row["created_at_ms"], "hosted": row["hosted_by"] is not None,
        "hostedBy": row["hosted_by"], "agentId": agent_id_for(row["entity_token"]), "displayName": None,
    }


def now_ms():
    return time.time_ns() // 1_000_000


def token_of(root, actor):
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        return db.execute("SELECT entity_token FROM agents WHERE actor = ?", (actor,)).fetchone()[0]


def seed(root):
    fixture = ROOT / "tests/fixtures/registry-bd6eb960"
    proof = json.loads((fixture / "provenance.json").read_text())
    for name, key in (("database.sql", "sqlSha256"), ("home.json", "homeSha256")):
        assert hashlib.sha256((fixture / name).read_bytes()).hexdigest() == proof[key]

    def expand(text):
        return text.replace("__HOME__", str(root / "home")).replace("__UID__", str(os.getuid()))

    for item in json.loads((fixture / "home.json").read_text()):
        path = root / "home" / item["path"]
        assert path.resolve().is_relative_to(root)
        if item["kind"] == "dir":
            path.mkdir(mode=int(item["mode"], 8))
        else:
            path.write_text(expand(item["text"]))
            path.chmod(int(item["mode"], 8))
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        db.executescript(expand((fixture / "database.sql").read_text()))


class Scenario:
    def __init__(self, root, production_socket):
        self.root, self.process, self.generation = root, None, 0
        self.socket = root / "state/daemon.sock"
        assert self.socket != production_socket
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
        for key, folder in {
            "HOME": "os-home", "HYPRIAL_HOME": "home", "HARNESS_STATE_DIR": "state",
            "UV_TOOL_DIR": "uv-tools", "UV_CACHE_DIR": "uv-cache", "XDG_CONFIG_HOME": "config",
            "TMPDIR": "tmp",
        }.items():
            path = root / folder
            assert path.resolve().is_relative_to(root)
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.env.update({
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "HYPRIAL_OWNER": "host-owner", "HYPRIAL_NODE_ID": "host-node",
            "HYPRIAL_USER_ID": paired_user_id("host-owner"),
            "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_USAGE_DISABLE": "1", "AT01_SOURCE": str(ROOT),
        })
        self.python = str(ROOT / ".venv/bin/python")
        assert Path(self.python).exists(), "create this worktree's test venv first"
        self.commands = []
        self.attempts = []
        SCENARIO_ROOTS.append(root)

    def start(self):
        """Start the isolated daemon and wait for readiness; every attempt leaves one record.

        Timing definition: the ledger entry is written before started_at, so its I/O
        is outside elapsed. elapsed runs from started_at (before the stdout/stderr
        files and Popen, as before) to t_obs, the parent's observation of the terminal
        state -- ping ready, poll() exit, loop end, or the moment an exception is
        caught -- taken before any evidence write or diagnostics. The readiness
        decision itself is unchanged; a ready observed past the deadline keeps a
        negative margin.
        """
        self.generation += 1
        label = f"daemon-{self.generation}"
        imports_path = self.root / (label + "-imports.json")
        dump_path = self.root / (label + "-sigusr2.txt")
        timing_path = self.root / (label + "-timing.json")
        env = dict(
            self.env,
            AT01_IMPORT_EVIDENCE=str(imports_path),
            AT01_FAULTHANDLER_EVIDENCE=str(dump_path),
            AT01_TIMING_EVIDENCE=str(timing_path),
            AT01_PROBE_EVIDENCE=str(self.root / (label + "-probe.jsonl")),
        )
        timeout_seconds = float(self.env.get("AT01_READINESS_TIMEOUT_SECONDS", "45"))
        attempt = {"label": label, "tree": self.env.get("AT01_SOURCE"), "budgetSeconds": timeout_seconds,
                   "startedUtc": utc_now(), "status": None, "observedUtc": None, "elapsedSeconds": None,
                   "popenToReadySeconds": None, "marginSeconds": None, "overrunSeconds": None,
                   "exitCode": None, "error": None, "diagnosticsSeconds": None,
                   "importTimingSnapshot": None, "importTimingReadOffsets": None,
                   "importTimingChangedDuringDiagnostics": None}
        try:
            self.attempts.append({"label": label, "startedUtc": attempt["startedUtc"]})
            write_json(self.root / "attempts.json", self.attempts)
        except Exception:
            pass  # evidence only
        started_at = clock()
        observed_at = None

        def observe(status, **fields):
            nonlocal observed_at
            observed_at = clock()
            attempt.update(status=status, observedUtc=utc_now(), **fields)

        try:
            with (self.root / (label + ".stdout")).open("w") as out, (self.root / (label + ".stderr")).open("w") as err:
                try:
                    self.process = subprocess.Popen(
                        [self.python, "-c", BOOTSTRAP, "daemon", "run", "--json"],
                        cwd=ROOT, env=env, stdout=out, stderr=err,
                        preexec_fn=lambda: os.nice(10),
                    )
                except OSError as error:
                    observe("spawn-error", error=f"{type(error).__name__}: {error}")
                    raise
            deadline = started_at + timeout_seconds
            while clock() < deadline:
                if self.process.poll() is not None:
                    observe("exited", exitCode=self.process.returncode)
                    raise AssertionError(f"daemon exited: {label}.stderr")
                try:
                    state = ping(self.socket)
                    if state.get("running") and not state.get("restorePending"):
                        seen_at, seen_utc = clock(), utc_now()
                        assert state["pid"] == self.process.pid
                        assert state["owner"] == "host-owner" and state["nodeId"] == "host-node"
                        observed_at = seen_at
                        attempt.update(status="ready", observedUtc=seen_utc)
                        return state
                except OSError:
                    pass
                sleep(0.1)
            observe("timeout")
            read_started = clock() - observed_at
            snapshot = _safe_timing(timing_path, imports_path)
            attempt.update(import_=snapshot, importTimingSnapshot="pre-diagnostics",
                           importTimingReadOffsets=[round(read_started, 6), round(clock() - observed_at, 6)])
            diagnosed_at = clock()
            try:
                detail = self._capture_readiness_timeout(label, started_at, imports_path, dump_path)
            except Exception as error:
                detail = f"diagnostics=failed-{type(error).__name__}"
            attempt["diagnosticsSeconds"] = round(clock() - diagnosed_at, 6)
            attempt["importTimingChangedDuringDiagnostics"] = _safe_timing(timing_path, imports_path) != snapshot
            raise AssertionError(f"isolated daemon readiness timeout; {detail}")
        except BaseException as error:
            if attempt["status"] is None:
                observe("error", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            self._write_attempt(label, attempt, started_at, observed_at, timing_path, imports_path)

    def _write_attempt(self, label, attempt, started_at, observed_at, timing_path, imports_path):
        """One terminal record per attempt; never changes the attempt's outcome."""
        try:
            record = {key: value for key, value in attempt.items() if key != "import_"}
            if observed_at is not None:
                elapsed = observed_at - started_at
                record["elapsedSeconds"] = round(elapsed, 6)
                if record["status"] == "ready":
                    record["popenToReadySeconds"] = round(elapsed, 6)
                    record["marginSeconds"] = round(record["budgetSeconds"] - elapsed, 6)
                elif record["status"] == "timeout":
                    record["overrunSeconds"] = round(max(elapsed - record["budgetSeconds"], 0.0), 6)
            imported = attempt.get("import_")
            if imported is None:
                imported = _safe_timing(timing_path, imports_path)
            record["import"] = {key: imported.get(key) for key in ("coreWall", "coreCpu", "allWall", "allCpu", "modules", "note")}
            write_json(self.root / (label + "-attempt.json"), record)
        except Exception:
            pass  # evidence only
        try:
            # The start probe's end point (criteria-v3.3 §4): the parent's own monotonic
            # reading at the terminal observation, comparable with the child's clock.
            if observed_at is not None:
                write_json(self.root / (label + "-ready-mark.json"),
                           {"status": attempt.get("status"), "monotonic": observed_at})
        except Exception:
            pass  # evidence only

    def _capture_readiness_timeout(self, label, started_at, imports_path, dump_path):
        process = self.process
        elapsed = time.monotonic() - started_at
        daemon_log = self.root / "state/logs/daemon.jsonl"
        imports = "present" if imports_path.exists() else "missing"
        try:
            with daemon_log.open() as lines:
                daemon_lines = sum(1 for _ in lines)
        except FileNotFoundError:
            daemon_lines = 0
        except Exception:
            daemon_lines = "unknown"

        signal_status = "not-sent"
        try:
            os.kill(process.pid, signal.SIGUSR2)
            signal_status = "sent"
            dump_deadline = time.monotonic() + 2
            while time.monotonic() < dump_deadline:
                if dump_path.exists() and dump_path.stat().st_size:
                    signal_status = "dump-written"
                    break
                time.sleep(0.05)
        except Exception as error:
            signal_status = f"failed-{type(error).__name__}"

        sample_path = self.root / (label + "-sample.txt")
        sample_error = ""
        if sys.platform.startswith("linux"):
            sample_status = "skipped-linux"
        else:
            try:
                sampled = subprocess.run(
                    ["/usr/bin/sample", str(process.pid), "3", "-file", str(sample_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    # The 3 s sample under a 5 s budget missed on every loaded-host red
                    # (09-28: tasks 39482/39563/39619/39674). This raises the wait budget
                    # by about 10 s on the timeout path only; it is not a hard bound.
                    timeout=15,
                )
                sample_status = f"exit-{sampled.returncode}"
                sample_error = sampled.stderr[-2000:]
            except Exception as error:
                sample_status = f"failed-{type(error).__name__}"
                sample_error = repr(error)
            if not sample_path.exists() or not sample_path.stat().st_size:
                try:
                    sample_path.write_text(
                        f"sample capture unavailable: {sample_status}\n{sample_error}"
                    )
                except Exception:
                    pass

        record = {
            "attempt": label + "-attempt.json",
            "imports": imports,
            "importsPath": str(imports_path),
            "daemonJsonlLines": daemon_lines,
            "daemonJsonlPath": str(daemon_log),
            "elapsedSeconds": round(elapsed, 3),
            "sigusr2": signal_status,
            "sigusr2Path": str(dump_path),
            "sample": sample_status,
            "sampleError": sample_error,
            "samplePath": str(sample_path),
        }
        try:
            write_json(self.root / (label + "-readiness-timeout.json"), record)
        except Exception:
            pass
        return (
            f"imports={imports}; daemon.jsonl lines={daemon_lines}; elapsed={elapsed:.3f}s; "
            f"sigusr2={signal_status}; sample={sample_status}"
        )

    def stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise AssertionError("owned daemon needed forced kill") from None
        assert code == 0, f"owned daemon exited {code}"

    def cli(self, *args, ok=True):
        """Run one CLI command. commands.json records its total call time against the
        30 s budget (not CLI import time); every outcome is recorded, then the
        original result or error stands."""
        entry = {"seq": len(self.commands) + 1, "args": args, "tree": self.env.get("AT01_SOURCE"),
                 "startedUtc": utc_now(), "budgetSeconds": CLI_BUDGET}
        started_at = clock()
        try:
            result = subprocess.run(
                [self.python, "-m", "hyprial.cli", *args, "--json"], cwd=ROOT,
                env=self.env, capture_output=True, text=True, timeout=CLI_BUDGET,
            )
        except subprocess.TimeoutExpired as error:
            seconds = clock() - started_at
            entry.update(status="timeout", exitCode=None, seconds=round(seconds, 6),
                         overrunSeconds=round(max(seconds - CLI_BUDGET, 0.0), 6),
                         stdout=_text(error.stdout), stderr=_text(error.stderr))
            self._record_command(entry)
            raise
        except OSError as error:
            entry.update(status="spawn-error", exitCode=None, seconds=round(clock() - started_at, 6),
                         error=f"{type(error).__name__}: {error}")
            self._record_command(entry)
            raise
        entry.update(status="ok" if result.returncode == 0 else "nonzero", exitCode=result.returncode,
                     seconds=round(clock() - started_at, 6), stdout=result.stdout, stderr=result.stderr)
        self._record_command(entry)
        if ok is not None:
            assert (result.returncode == 0) == ok, result.stdout + result.stderr
        answer = json.loads(result.stdout)
        if ok is not None:
            assert answer.get("ok") is ok, answer
        return answer

    def ensure_org_space(self, actor, prefix="hosted", timeout=30.0):
        """Create one org space and return its directory space id."""

        name = f"group-{prefix}-{uuid.uuid4().hex[:8]}"
        created = self.cli("org", "create", name, ok=None)
        space_id = created.get("spaceId", created.get("space_id"))
        if not isinstance(space_id, str) or not space_id:
            raise AssertionError(f"org create returned no space id: {created}")
        self.cli("org", "acl", "grant", name, actor)
        return space_id

    def _record_command(self, entry):
        self.commands.append(entry)
        try:
            write_json(self.root / "commands.json", self.commands)
        except Exception:
            pass  # evidence only; never mask the command's own outcome

    def run(self):
        seed(self.root)
        old = snapshot(self.root)
        self.start()
        # UUID P4 (U1, no compatibility window): the first open migrates the old
        # registry exactly as after_p4 states, and changes nothing else.
        migrated = user_agent.settled_snapshot(
            self.socket, snapshot, self.root, "host-owner", "host-node", agent_id_for)
        assert migrated == after_p4(old), "old registry rows or home changed on startup beyond P4"
        # U0c intentionally retires receipts at daemon.run; A3 separately checks
        # that CHECK migration itself preserves them before daemon.run.
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM lifecycle_receipts").fetchone() == (0,)
        created = self.cli("agent", "create", "--name", "fresh")
        agent = created["agent"]
        assert agent["actor"] == "fresh" and agent["displayName"] is None, agent
        assert agent["agentId"] == agent_id_for(token_of(self.root, "fresh")), agent
        assert agent["uri"] == f"agent:host-owner:host-node:{agent['agentId']}", agent
        before = snapshot(self.root)
        self.stop()
        self.start()
        assert snapshot(self.root) == before, "restart changed identity/home"
        listed = self.cli("agent", "list")
        assert any(row["uri"] == agent["uri"] for row in listed["agents"])
        write_json(self.root / "final-snapshot.json", snapshot(self.root))


def main(scenario_type=Scenario):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-socket", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="at01-e2e-", dir="/tmp")).resolve()
    report = {"scope": getattr(scenario_type, "scope", scenario_type.__name__ + "; no worker execution"), "root": str(root)}
    scenario = None
    # Positive completion marker: set only when scenario.run() actually returned.
    # PASS requires it; "no error field" alone never does.
    completed = False
    deferred = None
    try:
        report["productionBefore"] = production_identity(args.production_socket)
    except Exception:
        # No baseline means no comparison is possible; the scenario does not run
        # and the result can only be INCONCLUSIVE, never "identity changed".
        report["productionBeforeError"] = traceback.format_exc()
    try:
        if "productionBefore" in report:
            report["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
            (root / "source.patch").write_bytes(patch)
            report["patchSha256"] = hashlib.sha256(patch).hexdigest()
            report["runnerSha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            runner = Path(inspect.getfile(scenario_type))
            report["scenarioSha256"] = hashlib.sha256(runner.read_bytes()).hexdigest()
            scenario = scenario_type(root, args.production_socket.resolve())
            scenario.run()
            completed = True
    except Exception:
        report["traceback"] = traceback.format_exc()
    except BaseException as error:
        # SystemExit/KeyboardInterrupt from run() -- or from the setup before it,
        # so run() never started -- is neither FAIL nor PASS. Record the
        # interruption, still write and print the report, then re-raise below.
        report["interrupted"] = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        deferred = error
    finally:
        # Cleanup and the production check are independent: a failed stop() must
        # never skip the after-ping, the only evidence production was untouched.
        if scenario:
            try:
                scenario.stop()
            except Exception:
                report["cleanupError"] = traceback.format_exc()
        try:
            report["productionAfter"] = production_identity(args.production_socket)
        except Exception:
            report["productionCheckError"] = traceback.format_exc()
        # One fixed precedence; every error stays in its own field:
        # identity changed > production check > FAIL > scenario did not complete > cleanup > PASS.
        if ("productionBefore" in report and "productionAfter" in report
                and report["productionAfter"] != report["productionBefore"]):
            report["result"] = "INCONCLUSIVE: production identity changed"
        elif "productionBeforeError" in report or "productionCheckError" in report:
            report["result"] = "INCONCLUSIVE: production check"
        elif "traceback" in report:
            report["result"] = "FAIL"
        elif not completed:
            report["result"] = "INCONCLUSIVE: scenario did not complete"
        elif "cleanupError" in report:
            report["result"] = "INCONCLUSIVE: cleanup"
        else:
            report["result"] = "PASS"
        # Evidence writes are best effort: neither may suppress the printed report.
        try:
            report["ledgers"] = ledger_paths(root)
        except Exception:
            report["ledgersError"] = traceback.format_exc()
        try:
            write_json(root / "result.json", report)
        except Exception:
            report["resultWriteError"] = traceback.format_exc()
        print(json.dumps(report, indent=2))
    if deferred is not None:
        if isinstance(deferred, SystemExit) and deferred.code in (0, None):
            # A zero SystemExit would exit the process 0 while the result is not
            # PASS; re-raise as a generic failure, keeping the original as cause.
            raise SystemExit(1) from deferred
        raise deferred
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
