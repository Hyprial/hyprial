"""Subprocess fixtures for the post-install import/profile guards."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final


REPOSITORY: Final = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)
SOURCE_PACKAGE: Final = REPOSITORY / "src" / "hyprial"
SURFACE_DATA: Final = Path(__file__).with_name("post_install_surface.json")
LAZY_TARGET: Final = "hyprial.daemon.impl.configuration.home_guard"
SCENARIOS: Final = (
    "reconciled",
    "not-reconciled",
    "child-exited",
    "hard-cap",
    "old-daemon",
    "wrong-version",
    "supervised",
    "unsupervised",
    "gui-present",
    "gui-absent",
    "steps-pending",
    "steps-none",
    "steps-failing",
)


def surface_data() -> dict[str, object]:
    return json.loads(SURFACE_DATA.read_text(encoding="utf-8"))


def copy_package(destination: Path) -> Path:
    root = destination / "package"
    shutil.copytree(
        SOURCE_PACKAGE,
        root / "hyprial",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return root


def _module_path(package_root: Path, module: str) -> Path:
    relative = module.removeprefix("hyprial.").replace(".", "/")
    return package_root / "hyprial" / f"{relative}.py"


def _definition_qualname(tree: ast.AST, wanted: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def walk(body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, node.name))
                if qualname == wanted:
                    found.append(node)
                walk(node.body, (*prefix, node.name, "<locals>"))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, (*prefix, node.name))

    walk(getattr(tree, "body", []), ())
    assert len(found) == 1, (wanted, len(found))
    return found[0]


def mutate_function(package_root: Path, function_id: str) -> None:
    mutate_function_statement(
        package_root,
        function_id,
        "from hyprial.daemon import live_daemon_pid\n_ = live_daemon_pid",
    )


def mutate_function_statement(
    package_root: Path, function_id: str, statement: str
) -> None:
    module, qualname = function_id.split(":", 1)
    path = _module_path(package_root, module)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = _definition_qualname(tree, qualname)
    first_body_line = function.body[0].lineno - 1
    lines = source.splitlines(keepends=True)
    indent = " " * (function.col_offset + 4)
    injected = "".join(f"{indent}{line}\n" for line in statement.splitlines())
    lines.insert(first_body_line, injected)
    replacement = path.with_suffix(".mutated.py")
    replacement.write_text("".join(lines), encoding="utf-8")
    os.replace(replacement, path)


def add_called_helper(package_root: Path) -> str:
    module = "hyprial.shell.impl.cli.commands.common.pending_restart"
    path = _module_path(package_root, module)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = _definition_qualname(tree, "_verify_restart_verdict")
    lines = source.splitlines(keepends=True)
    indent = " " * (function.col_offset + 4)
    lines.insert(function.body[0].lineno - 1, f"{indent}_profile_added_helper()\n")
    lines.append("\n\ndef _profile_added_helper() -> None:\n    return None\n")
    path.write_text("".join(lines), encoding="utf-8")
    return f"{module}:_profile_added_helper"


def _environment(package_root: Path, new_package_root: Path | None = None) -> dict[str, str]:
    environment = {
        **os.environ,
        "HOME": str(package_root.parent / "user-home"),
        "HYPRIAL_HOME": str(package_root.parent / "home"),
        "HARNESS_STATE_DIR": str(package_root.parent / "state"),
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(package_root),
    }
    if new_package_root is not None:
        environment["NEW_PACKAGE_ROOT"] = str(new_package_root)
    return environment


_STATIC_SCAN = r'''
import ast
import importlib.util
import json
from pathlib import Path
import sys

package_root = Path(sys.argv[1])
request = json.loads(sys.stdin.read())
modules = request["modules"]
primed = frozenset(request["primed"])

facades = {}
for interface_path in sorted((package_root / "hyprial").glob("*/interface.py")):
    tree = ast.parse(interface_path.read_text(encoding="utf-8"))
    has_getattr = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__getattr__"
        for node in tree.body
    )
    tables = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_FACADE_EXPORTS" for target in node.targets)
    ]
    if not has_getattr or len(tables) != 1:
        continue
    table = ast.literal_eval(tables[0])
    domain = interface_path.parent.name
    facades[f"hyprial.{domain}"] = table
    facades[f"hyprial.{domain}.interface"] = table


def resolved_from(module_name, level, imported):
    if level:
        package = module_name.rpartition(".")[0]
        base = importlib.util.resolve_name("." * level + (imported or ""), package)
    else:
        base = imported or ""
    return base


def import_bindings(nodes, module_name):
    facades_by_name = {}
    importlib_names = set()
    import_module_names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if alias.name in facades:
                    facades_by_name[bound] = alias.name
                if alias.name == "importlib":
                    importlib_names.add(bound)
        elif isinstance(node, ast.ImportFrom):
            base = resolved_from(module_name, node.level, node.module)
            for alias in node.names:
                bound = alias.asname or alias.name
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in facades:
                    facades_by_name[bound] = candidate
                if base == "importlib" and alias.name == "import_module":
                    import_module_names.add(bound)
    return facades_by_name, importlib_names, import_module_names


def child_nodes_without_nested(function):
    pending = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            pending.append(child)


def functions(tree):
    found = []
    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, node.name))
                found.append((qualname, node))
                walk(node.body, (*prefix, node.name, "<locals>"))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, (*prefix, node.name))
    walk(tree.body, ())
    return found


def attribute_parts(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]

findings = []
for module_name in modules:
    relative = module_name.removeprefix("hyprial.").replace(".", "/")
    path = package_root / "hyprial" / f"{relative}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    module_facades, module_importlib, module_import_module = import_bindings(module_nodes, module_name)
    for qualname, function in functions(tree):
        nodes = list(child_nodes_without_nested(function))
        local_facades, local_importlib, local_import_module = import_bindings(nodes, module_name)
        facade_bindings = {**module_facades, **local_facades}
        importlib_names = module_importlib | local_importlib
        import_module_names = module_import_module | local_import_module
        function_id = f"{module_name}:{qualname}"
        seen = set()
        def add(line, target, reason):
            key = (line, target, reason)
            if key not in seen:
                seen.add(key)
                findings.append({
                    "function": function_id,
                    "line": line,
                    "target": target,
                    "reason": reason,
                })
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hyprial") and alias.name not in primed:
                        add(node.lineno, alias.name, "unprimed import")
            elif isinstance(node, ast.ImportFrom):
                base = resolved_from(module_name, node.level, node.module)
                if base.startswith("hyprial") and base not in primed:
                    add(node.lineno, base, "unprimed from-import")
                table = facades.get(base)
                if table is not None:
                    for alias in node.names:
                        if alias.name == "*":
                            add(node.lineno, base, "cannot verify facade star import")
                            continue
                        provider = table.get(alias.name)
                        if provider is not None and provider[0] not in primed:
                            add(node.lineno, provider[0], f"unprimed lazy facade export {base}.{alias.name}")
                elif base.startswith("hyprial"):
                    for alias in node.names:
                        candidate = f"{base}.{alias.name}"
                        relative = candidate.replace(".", "/")
                        is_module = (
                            (package_root / f"{relative}.py").is_file()
                            or (package_root / relative / "__init__.py").is_file()
                        )
                        if is_module and candidate not in primed:
                            add(node.lineno, candidate, "unprimed imported submodule")
            elif isinstance(node, ast.Attribute):
                parts = attribute_parts(node)
                if parts and parts[0] in facade_bindings and len(parts) >= 2:
                    facade = facade_bindings[parts[0]]
                    provider = facades[facade].get(parts[1])
                    if provider is not None and provider[0] not in primed:
                        add(node.lineno, provider[0], f"unprimed lazy facade attribute {facade}.{parts[1]}")
            elif isinstance(node, ast.Call):
                dynamic = False
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in import_module_names | {"__import__"}
                ):
                    dynamic = True
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in importlib_names
                ):
                    dynamic = True
                if not dynamic:
                    continue
                argument = node.args[0] if node.args else None
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    target = argument.value
                    if target.startswith("."):
                        package_arg = node.args[1] if len(node.args) > 1 else None
                        if isinstance(package_arg, ast.Constant) and isinstance(package_arg.value, str):
                            target = importlib.util.resolve_name(target, package_arg.value)
                        else:
                            add(node.lineno, target, "cannot verify relative dynamic import")
                            continue
                    if target.startswith("hyprial") and target not in primed:
                        add(node.lineno, target, "unprimed literal dynamic import")
                elif module_name in facades and qualname == "__getattr__":
                    # This canonical table-driven loader is resolved at every facade
                    # use above. Treating its table key as arbitrary would lose the
                    # call-site name that selects one provider.
                    continue
                else:
                    add(node.lineno, "<dynamic>", "cannot verify dynamic import")

print(json.dumps({"findings": findings, "primed": sorted(primed)}))
'''


_PROFILE_TAIL = r'''
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

package_root, mode, scenario = map(str, sys.argv[1:])
sys.path.insert(0, package_root)
import hyprial.shell.impl.cli.runtime.entry  # installs the explicit CLI composition
from hyprial.shell.impl.cli.commands.common import support as _support
from hyprial.shell.impl.cli.commands.common.services import get_services as _get_services
_services = _get_services()

prime_result = _services._prime_post_install_restart_imports()
primed = prime_result if isinstance(prime_result, frozenset) else frozenset(
    name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")
)
if mode == "stale":
    shutil.copytree(
        Path(os.environ["NEW_PACKAGE_ROOT"]) / "hyprial",
        Path(package_root) / "hyprial",
        dirs_exist_ok=True,
    )
else:
    assert "hyprial.daemon.impl.configuration.home_guard" not in primed
    class RejectUnprimedHyprialModules:
        def find_spec(self, fullname, path=None, target=None):
            del path, target
            if fullname.startswith("hyprial.") and fullname not in primed:
                raise ImportError("post-install import fence refused: " + fullname)
            return None
    sys.meta_path.insert(0, RejectUnprimedHyprialModules())

pending = sys.modules["hyprial.shell.impl.cli.commands.common.pending_restart"]
service = sys.modules["hyprial.shell.impl.cli.commands.daemon.service"]
alert = sys.modules["hyprial.daemon.impl.autoupdate.alert"]
emit = sys.modules["hyprial.shell.impl.cli.output.emit"]
root = Path(package_root).parent
home = root / "home"
state_dir = root / "state"
(home / "secrets").mkdir(parents=True, exist_ok=True)
state_dir.mkdir(parents=True, exist_ok=True)
(home / "channels.json").write_text(json.dumps({
    "version": 1,
    "gateways": [{"provider": "lark", "name": "owner", "appId": "app",
        "credentialRef": "lark-owner",
        "routes": [{"name": "dm", "type": "direct", "nativeId": "ou_owner"}]}],
}), encoding="utf-8")
(home / "secrets" / "lark-owner.json").write_text(
    json.dumps({"appSecret": "secret"}), encoding="utf-8"
)
(state_dir / "users.json").write_text(json.dumps({
    "version": 1,
    "users": [{"owner": "owner", "ownerKey": "owner", "loginName": "owner@example.test",
        "agents": [], "preferredReceiver": {"machine": "m", "machineKey": "m"},
        "notificationRules": [],
        "ownerOpenId": {"channel": "owner", "openId": "ou_owner", "boundAt": "2026-01-01T00:00:00Z"}}],
}), encoding="utf-8")

before = SimpleNamespace(
    platform="launchd", unit="com.hyprial.hyprial.daemon", unit_path=root / "service.plist",
    installed=scenario != "unsupervised", loaded=scenario != "unsupervised",
    running=True, pid=4101, boot_persistent=True, changed=None, attributed=True,
    template_matches=True, serving_pid=4101, supervision="supervised", relaunch_count=1,
    last_exit_code=0, ownership_busy=False, ownership_reason=None, backup_path=None,
)
after = SimpleNamespace(**{**vars(before), "pid": 4202, "serving_pid": 4202})
exited = SimpleNamespace(**{
    **vars(after), "running": False, "serving_pid": None,
    "supervision": "lost", "last_exit_code": 17,
})
manager_failure = {"enabled": scenario == "child-exited"}
class Manager:
    def inspect(self, serving_pid=None):
        del serving_pid
        return before
    def restart(self, serving_pid, *, timeout_seconds=None):
        del serving_pid
        assert timeout_seconds == _support.DAEMON_START_HARD_CAP_SECONDS
        return exited if manager_failure["enabled"] else after
service._service_manager = lambda: Manager()

correct = {"running": True, "pid": 4202, "epoch": "new", "version": "0.4.0.dev2",
    "phase": "serving" if scenario == "not-reconciled" else "reconciled"}
def probe(timeout=0.5):
    del timeout
    if scenario == "hard-cap":
        raise OSError("still unavailable at hard cap")
    if scenario == "old-daemon":
        return {**correct, "pid": 4101, "epoch": "old"}
    if scenario == "wrong-version":
        return {**correct, "version": "0.4.0.dev1"}
    return correct
_services._daemon_probe = probe

def daemon_request(method, *args, **kwargs):
    del args, kwargs
    if method == "ping":
        return correct
    if method == "ps":
        return {"connectors": [{"running": False}, {"running": True}]}
    raise AssertionError(method)
_support._daemon_request = daemon_request
_services._stop_daemon_gracefully = lambda: None

def launch(*, ready_timeout):
    assert ready_timeout == _support.DAEMON_START_HARD_CAP_SECONDS
    if scenario == "child-exited":
        error = _support.CliError("DAEMON_START_FAILED", "daemon child exited during startup")
        error._daemon_process_pid = 4202
        error._daemon_process_exited = True
        raise error
    if scenario == "hard-cap":
        error = _support.CliError("DAEMON_START_TIMEOUT", "hard cap passed")
        error._daemon_process_pid = 4202
        error._daemon_process_identity = "birth"
        raise error
    return pending.DaemonLaunchResult.existing(probe())
_services._launch_daemon_process = launch

sent = []
class Sender:
    def send_owner_dm(self, open_id, text, *, idempotency_key):
        sent.append([open_id, text, idempotency_key])
        return "om_guard"
if mode == "stale":
    sdk = sys.modules.get("hyprial.daemon.impl.adapters.lark.outbound.sdk")
    if sdk is not None:
        class Gateway:
            @classmethod
            def from_credentials(cls, app_id, secret, **kwargs):
                del app_id, secret, kwargs
                return Sender()
        sdk.LarkSdkGateway = Gateway
else:
    def notify_failure(**kwargs):
        return alert.notify_upgrade_failure(**kwargs, sender_factory=lambda _app_id, _secret: Sender())
    _services.notify_upgrade_failure = notify_failure
_services._hyprial_home = lambda: home
_services._state_dir = lambda: state_dir

package_path = (Path(package_root) / "hyprial").resolve()
traced = set()
def profile(frame, event, arg):
    del arg
    if event != "call":
        return
    try:
        filename = Path(frame.f_code.co_filename).resolve()
        filename.relative_to(package_path)
    except (OSError, ValueError):
        return
    qualname = frame.f_code.co_qualname
    if "<" not in qualname:
        traced.add(f"{frame.f_globals.get('__name__')}:{qualname}")

sys.setprofile(profile)
resolution = SimpleNamespace(tag="v0.4.0.dev2", commit="d" * 40)
result = {"ok": True, "resolvedTag": resolution.tag, "resolvedCommit": resolution.commit}
failure = None
try:
    outcome = pending._restart_daemon_onto_install(
        result,
        before={"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"},
        installed_version="0.4.0.dev2", resolution=resolution,
        resolved={"resolvedTag": resolution.tag, "resolvedCommit": resolution.commit},
    )
except _support.CliError as error:
    failure = error
    outcome = error.data

rendered = io.StringIO()
with contextlib.redirect_stdout(rendered):
    emit.emit_result(outcome, json_output=False)

# The non-reconciled profile also traverses the failure/alert observation branch.
# This makes both reviewer mutations executable under the same fenced drill.
if scenario == "not-reconciled" or mode == "stale":
    manager_failure["enabled"] = True
    try:
        pending._restart_daemon_onto_install(
            {"ok": True},
            before={"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"},
            installed_version="0.4.0.dev2", resolution=resolution,
            resolved={"resolvedTag": resolution.tag, "resolvedCommit": resolution.commit},
        )
    except _support.CliError as error:
        failure = error
sys.setprofile(None)

if failure is not None:
    alert_result = failure.data.get("alert", {}) if isinstance(failure.data, dict) else {}
    alert_reason = str(alert_result.get("reason", ""))
    if not sent and ((mode == "stale" and "ImportError" in alert_reason)
        or "post-install import fence refused" in alert_reason):
        raise ImportError(alert_reason)
if scenario in {"reconciled", "supervised", "unsupervised", "not-reconciled"}:
    assert result["restart"]["restarted"] is True
    assert result["restart"]["after"]["pid"] == 4202
    assert result["restart"]["after"]["epoch"] == "new"
    assert result["restart"]["after"]["version"] == "0.4.0.dev2"
if scenario == "not-reconciled":
    assert result["restart"]["confirmed"] is False
    assert "1 connectors pending" in result["restart"]["reason"]
if scenario in {"child-exited", "hard-cap", "old-daemon", "wrong-version"}:
    assert failure is not None and failure.code == "UPGRADE_RESTART_FAILED"
    assert sent
if mode == "stale":
    assert sent and "resolvedTag:" in rendered.getvalue()
print(json.dumps({
    "primed": sorted(primed), "trace": sorted(traced), "rendered": rendered.getvalue(),
    "alertCount": len(sent), "failed": failure is not None,
}))
'''


_TRUE_ROOT_PROFILE = r'''
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

package_root, mode, scenario = map(str, sys.argv[1:])
sys.path.insert(0, package_root)

# A source mutation can execute while the primer builds the old-module map.
# Reject the dedicated mutation target from process start so it cannot become
# resident before the general post-swap fence is activated.
blocked_imports = []
mutation_target = "hyprial.daemon.impl.configuration.home_guard"
class RejectMutationTarget:
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        if fullname == mutation_target:
            blocked_imports.append(fullname)
            raise ImportError(
                "post-install import fence blocked attempts: " + fullname
            )
        return None
mutation_fence = RejectMutationTarget()
sys.meta_path.insert(0, mutation_fence)

import hyprial.shell.impl.cli.runtime.entry  # installs the explicit CLI composition
from hyprial.shell.impl.cli.commands.common import support as _support
from hyprial.shell.impl.cli.commands.common.services import get_services as _get_services
_services = _get_services()

# The old interpreter resolves every dependency before the destructive install.
prime_result = _services._prime_post_install_restart_imports()
primed = prime_result if isinstance(prime_result, frozenset) else frozenset(
    name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")
)
assert "hyprial.daemon.impl.configuration.home_guard" not in primed

def old_module(name):
    # This fixture dependency is loaded from the OLD tree before the swap. It
    # must not assume that a newer primer already includes the r5 surface.
    return sys.modules.get(name) or importlib.import_module(name)

commands = old_module("hyprial.shell.impl.cli.commands.upgrade.commands")
pending = old_module("hyprial.shell.impl.cli.commands.common.pending_restart")
service = old_module("hyprial.shell.impl.cli.commands.daemon.service")
alert = old_module("hyprial.daemon.impl.autoupdate.alert")
updates = old_module("hyprial.daemon.impl.updates")
old_module("hyprial.shell.impl.gui.apps")
upgrade_runner = old_module("hyprial.daemon.impl.upgrade.runner")
upgrade_steps = old_module("hyprial.daemon.impl.upgrade.steps")

# Record every replacement of a loaded product function. M2 may replace only
# true process/socket/service/transport boundaries, never a function in the M1
# post-install surface. This makes the rule executable instead of relying on a
# reviewer noticing a new assignment in this fixture.
def product_callable_bindings():
    bindings = {}
    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith("hyprial.") or module is None:
            continue
        for name, value in tuple(vars(module).items()):
            provider = getattr(value, "__module__", "")
            qualname = getattr(value, "__qualname__", "")
            if provider.startswith("hyprial.") and qualname and "<" not in qualname:
                bindings[(module_name, name)] = (value, f"{provider}:{qualname}")
    return bindings

original_product_callables = product_callable_bindings()
root = Path(package_root).parent
home = root / "home"
state_dir = root / "state"
(home / "secrets").mkdir(parents=True, exist_ok=True)
state_dir.mkdir(parents=True, exist_ok=True)
(home / "channels.json").write_text(json.dumps({
    "version": 1,
    "gateways": [{"provider": "lark", "name": "owner", "appId": "app",
        "credentialRef": "lark-owner",
        "routes": [{"name": "dm", "type": "direct", "nativeId": "ou_owner"}]}],
}), encoding="utf-8")
(home / "secrets" / "lark-owner.json").write_text(
    json.dumps({"appSecret": "secret"}), encoding="utf-8"
)
(state_dir / "users.json").write_text(json.dumps({
    "version": 1,
    "users": [{"owner": "owner", "ownerKey": "owner", "loginName": "owner@example.test",
        "agents": [], "preferredReceiver": {"machine": "m", "machineKey": "m"},
        "notificationRules": [],
        "ownerOpenId": {"channel": "owner", "openId": "ou_owner", "boundAt": "2026-01-01T00:00:00Z"}}],
}), encoding="utf-8")

# Pre-install resolution and uv are below the guarded boundary. The fake
# install is the only place that activates profiling and the import fence.
resolution = SimpleNamespace(tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2")
commands._resolve_upgrade_target = lambda **_kwargs: (
    "https://example.invalid/hyprial.git", None, resolution,
    {"resolvedTag": resolution.tag, "resolvedCommit": resolution.commit,
        "source": "official", "persistentUpdateSource": "https://example.invalid/hyprial.git"},
)
installation = SimpleNamespace(
    url="git+https://example.invalid/hyprial.git", version="0.4.0.dev1",
    requested_revision="old", commit="c" * 40,
)
updates.read_installation = lambda: installation
updates.installation_is_local = lambda _installation: False
updates.uv_tool_dir_guard = lambda: {
    "allowed": True, "toolDir": str(root / "tool"), "sysPrefix": str(root / "tool")
}

before_fact = {"pid": 4101, "epoch": "old", "version": "0.4.0.dev1"}
_services._running_daemon_before_upgrade = lambda _version: before_fact
_services.require_initialized_hyprial_home = lambda: None
_services._hyprial_home = lambda: home
_services._state_dir = lambda: state_dir

package_path = (Path(package_root) / "hyprial").resolve()
traced = set()
def profile(frame, event, arg):
    del arg
    if event != "call":
        return
    try:
        filename = Path(frame.f_code.co_filename).resolve()
        filename.relative_to(package_path)
    except (OSError, ValueError):
        return
    qualname = frame.f_code.co_qualname
    if "<" not in qualname:
        traced.add(f"{frame.f_globals.get('__name__')}:{qualname}")

class RejectUnprimedHyprialModules:
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        if fullname.startswith("hyprial.") and fullname not in primed:
            blocked_imports.append(fullname)
            raise ImportError("post-install import fence refused: " + fullname)
        return None

fence = RejectUnprimedHyprialModules()
def install_boundary(**_kwargs):
    if mode == "stale":
        shutil.copytree(
            Path(os.environ["NEW_PACKAGE_ROOT"]) / "hyprial",
            Path(package_root) / "hyprial",
            dirs_exist_ok=True,
        )
    else:
        sys.meta_path.insert(0, fence)
    sys.setprofile(profile)
    completed = subprocess.CompletedProcess(["uv"], 0, stdout="installed", stderr="")
    installed = SimpleNamespace(version="0.4.0.dev2", commit=resolution.commit)
    return completed, installed
commands._install_locked_requirement = install_boundary

# One service-manager seam drives supervised and direct restart through the
# shared product verdict. Process, socket, and transport are the IO seams.
before = SimpleNamespace(
    platform="launchd", unit="com.hyprial.hyprial.daemon", unit_path=root / "service.plist",
    installed=scenario != "unsupervised", loaded=scenario != "unsupervised",
    running=True, pid=4101, boot_persistent=True, changed=None, attributed=True,
    template_matches=True, serving_pid=4101, supervision="supervised", relaunch_count=1,
    last_exit_code=0, ownership_busy=False, ownership_reason=None, backup_path=None,
)
after = SimpleNamespace(**{**vars(before), "pid": 4202, "serving_pid": 4202})
exited = SimpleNamespace(**{
    **vars(after), "running": False, "serving_pid": None,
    "supervision": "lost", "last_exit_code": 17,
})
class Manager:
    def inspect(self, serving_pid=None):
        del serving_pid
        return before
    def restart(self, serving_pid, *, timeout_seconds=None):
        del serving_pid
        assert timeout_seconds == _support.DAEMON_START_HARD_CAP_SECONDS
        return exited if scenario == "child-exited" else after
service._service_manager = lambda: Manager()

correct = {"running": True, "pid": 4202, "epoch": "new", "version": "0.4.0.dev2",
    "phase": "serving" if scenario == "not-reconciled" else "reconciled"}
def probe(timeout=0.5):
    del timeout
    if scenario == "hard-cap":
        raise OSError("still unavailable at hard cap")
    if scenario == "old-daemon":
        return {**correct, "pid": 4101, "epoch": "old"}
    if scenario == "wrong-version":
        return {**correct, "version": "0.4.0.dev1"}
    return correct
_services._daemon_probe = probe

def daemon_request(method, *args, **kwargs):
    del args, kwargs
    if method == "ping":
        return correct
    if method == "ps":
        return {"connectors": [{"running": False}, {"running": True}]}
    raise AssertionError(method)
_support._daemon_request = daemon_request
_services._stop_daemon_gracefully = lambda: None

def launch(*, ready_timeout):
    assert ready_timeout == _support.DAEMON_START_HARD_CAP_SECONDS
    if scenario == "child-exited":
        error = _support.CliError("DAEMON_START_FAILED", "daemon child exited during startup")
        error._daemon_process_pid = 4202
        error._daemon_process_exited = True
        raise error
    if scenario == "hard-cap":
        error = _support.CliError("DAEMON_START_TIMEOUT", "hard cap passed")
        error._daemon_process_pid = 4202
        error._daemon_process_identity = "birth"
        raise error
    return pending.DaemonLaunchResult.existing(probe())
_services._launch_daemon_process = launch

sent = []
class Sender:
    def send_owner_dm(self, open_id, text, *, idempotency_key):
        sent.append([open_id, text, idempotency_key])
        return "om_guard"
class Gateway:
    @classmethod
    def from_credentials(cls, app_id, secret, **kwargs):
        del app_id, secret, kwargs
        return Sender()
sdk = sys.modules.get("hyprial.daemon.impl.adapters.lark.outbound.sdk")
if sdk is not None:
    sdk.LarkSdkGateway = Gateway
alert.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(
    args[0] if args else [], 0,
    stdout=json.dumps({"ok": True, "packageVersion": "0.4.0.dev2"}), stderr="",
)

# Keep a complete legacy wheel payload as a negative control: retirement must
# ignore it, not unpack or reactivate it. Execute the real apps.upgrade and
# _unavailable bodies under the fence for both payload-present and absent cases.
if scenario == "gui-present":
    bundle_dir = Path(package_root) / "hyprial" / "_gui"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    artifact = bundle_dir / "gui.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        for relative, payload, mode_bits in (
            ("hyprial-install.json", b'{"start":["bash","start.sh"]}', 0o644),
            ("start.sh", b'#!/bin/sh\nexit 0\n', 0o755),
            ("package.json", b'{"name":"@hyprial/gui"}', 0o644),
            ("package-lock.json", b'{"lockfileVersion":3}', 0o644),
        ):
            member = tarfile.TarInfo(f"gui-0.4.0.dev2/{relative}")
            member.size = len(payload)
            member.mode = mode_bits
            archive.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (bundle_dir / "release.json").write_text(json.dumps({
        "schema": "hyprial.product-gui/v1", "version": "0.4.0.dev2",
        "productVersion": "0.4.0.dev2", "commit": resolution.commit,
        "sha256": digest, "file": "gui.tar.gz",
        "manifest": "hyprial-install.json",
    }), encoding="utf-8")

# The real locked runner sees no steps, one successful pending step, or one
# failing pending step. The step callable is data supplied to that runner.
def successful_step(_context):
    return {"migrated": True}
def failing_step(_context):
    raise RuntimeError("synthetic step failure")
if scenario == "steps-pending":
    upgrade_runner.STEPS = (upgrade_steps.UpgradeStep("guard-success", successful_step),)
elif scenario == "steps-failing":
    upgrade_runner.STEPS = (upgrade_steps.UpgradeStep("guard-failure", failing_step),)
else:
    upgrade_runner.STEPS = ()

current_product_callables = product_callable_bindings()
stubbed_product_functions = sorted({
    function_id
    for binding, (original, function_id) in original_product_callables.items()
    if binding not in current_product_callables
    or current_product_callables[binding][0] is not original
})

captured = io.StringIO()
exit_code = 0
caught = None
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    try:
        commands.upgrade(
            SimpleNamespace(invoked_subcommand=None),
            tag=None, source="official", ref=None, force=True,
            no_restart=False, json_output=True,
        )
    except BaseException as error:
        caught = error
        exit_code = int(getattr(error, "exit_code", getattr(error, "code", 1)) or 1)

# A5 always executes the real failure-alert body after the swap. The OLD base
# discovers the NEW SDK here and records its stale-kernel ImportError; the fixed
# primer keeps the old SDK module resident, so only its transport is replaced.
stale_alert = None
if mode == "stale":
    stale_alert = alert.notify_upgrade_failure(
        hyprial_home=home, state_dir=state_dir, host="guard-host",
        summary="synthetic restart failure", detail="synthetic failure detail",
        idempotency_key="guard-stale-alert",
    )
sys.setprofile(None)
if fence in sys.meta_path:
    sys.meta_path.remove(fence)
if mutation_fence in sys.meta_path:
    sys.meta_path.remove(mutation_fence)

# Product error handling is allowed to catch ImportError, but that must never
# hide an attempted post-swap module load from this harness-owned side channel.
if blocked_imports:
    raise ImportError(
        "post-install import fence blocked attempts: " + ", ".join(blocked_imports)
    )

rendered = captured.getvalue()
if mode == "stale" and stale_alert is not None:
    stale_reason = str(stale_alert.reason or "")
    if "ImportError" in stale_reason:
        raise ImportError(stale_reason)
if isinstance(caught, ImportError) and "post-install import fence refused" in str(caught):
    raise caught
if "post-install import fence refused" in rendered:
    raise ImportError(rendered)
lines = [line for line in rendered.splitlines() if line.startswith("{")]
assert lines, rendered
result = json.loads(lines[-1])
failed = result.get("ok") is False
if mode != "stale" and scenario in {
    "child-exited", "hard-cap", "old-daemon", "wrong-version"
}:
    assert failed and exit_code == 1
    assert result["code"] == "UPGRADE_RESTART_FAILED"
elif mode != "stale":
    assert not failed and exit_code == 0
    assert result["restart"]["restarted"] is True
    assert result["restart"]["after"] == {
        "pid": 4202, "epoch": "new", "version": "0.4.0.dev2"
    }
if scenario in {"gui-present", "gui-absent"}:
    assert result["gui"]["ok"] is False
    assert result["gui"]["code"] == "GUI_UNAVAILABLE"
    assert "temporarily unavailable" in result["gui"]["error"]
    assert not (home / "apps").exists()
if scenario == "gui-present":
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
if scenario == "steps-pending":
    assert result["upgradeSteps"]["pending"] == []
if scenario == "steps-none":
    assert result["upgradeSteps"] == {"steps": [], "pending": []}
if scenario == "steps-failing":
    assert result["upgradeSteps"]["pending"] == ["guard-failure"]
if mode == "stale" and "ImportError" in rendered:
    raise ImportError(rendered)
print(json.dumps({
    "primed": sorted(primed), "trace": sorted(traced), "rendered": rendered,
    "alertCount": len(sent), "failed": failed, "blockedImports": blocked_imports,
    "stubbedProductFunctions": stubbed_product_functions,
}))
'''


def run_static_scan(package_root: Path, modules: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _STATIC_SCAN, str(package_root)],
        cwd=package_root.parent,
        env=_environment(package_root),
        input=json.dumps({"modules": modules, "primed": surface_data()["primedModules"]}),
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


def run_profile(
    package_root: Path,
    scenario: str,
    *,
    mode: str = "fenced",
    new_package_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert scenario in SCENARIOS
    return subprocess.run(
        [sys.executable, "-c", _TRUE_ROOT_PROFILE, str(package_root), mode, scenario],
        cwd=package_root.parent,
        env=_environment(package_root, new_package_root),
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
