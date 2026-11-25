"""Admission tests for the source GUI backend selection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui.backend.admission import SourceAdmission, admit_source
from hyprial.shell.impl.gui.errors import GuiError


OWNER = "source-owner"
NODE = "source-node"
ACTOR = f"user:{OWNER}"


def _home(tmp_path: Path, *, owner: str | None = OWNER) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings: dict[str, Any] = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home.parent / "user"),
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_NODE_ID": NODE,
        "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
    }


def _catalog() -> dict[str, Any]:
    entries = daemon_catalog()["methods"]
    for entry in entries:
        entry["declared"] = True
        entry["state"] = "available"
        entry["callable"] = True
        entry["reason"] = None
    return {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "epoch": "epoch-1",
        "packageVersion": __version__,
        "methods": entries,
    }


def _ping(home: Path) -> dict[str, Any]:
    return {
        "running": True,
        "pid": 123,
        "version": __version__,
        "nodeId": NODE,
        "owner": OWNER,
        "phase": "serving",
        "restorePending": False,
        "epoch": "epoch-1",
        "socket": str(home / "state" / "daemon.sock"),
    }


def _request_once(ping: dict[str, Any] | None = None, catalog: dict[str, Any] | None = None):
    responses = {"ping": ping, "daemon.catalog": catalog}

    def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = responses[method]
        if isinstance(response, Exception):
            raise response
        assert response is not None
        return response

    return request


def test_daemon_admission_derives_principal_from_verified_identity(tmp_path: Path) -> None:
    home = _home(tmp_path)
    admission = admit_source(
        _environment(home),
        request_once=_request_once(_ping(home), _catalog()),
    )

    assert admission.driver == "daemon"
    assert admission.home == home.resolve()
    assert admission.state_dir == (home / "state").resolve()
    assert admission.socket_path == (home / "state" / "daemon.sock").resolve()
    assert admission.owner == OWNER
    assert admission.node == NODE
    assert admission.principal == ACTOR


@pytest.mark.parametrize("override", [OWNER, "different-owner", ""])
def test_daemon_admission_rejects_any_owner_override(
    tmp_path: Path, override: str
) -> None:
    home = _home(tmp_path)
    environ = _environment(home)
    environ["HYPRIAL_OWNER"] = override
    called = False

    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        raise AssertionError("daemon must not be contacted after an owner override")

    with pytest.raises(GuiError) as caught:
        admit_source(environ, request_once=request)

    assert caught.value.code == "GUI_OWNER_OVERRIDE"
    assert called is False


def test_daemon_admission_rejects_missing_home_before_contact(tmp_path: Path) -> None:
    environ = _environment(tmp_path / "missing")
    with pytest.raises(GuiError) as caught:
        admit_source(environ, request_once=lambda *args, **kwargs: {})
    assert caught.value.code == "GUI_HOME_NOT_INITIALIZED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows access control")
@pytest.mark.parametrize("owner", [None, OWNER])
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770])
def test_nonprivate_home_is_refused_before_identity_or_daemon_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str | None, mode: int,
) -> None:
    from hyprial.shell.impl.gui.backend import admission as boundary

    home = _home(tmp_path, owner=owner)
    home.chmod(mode)
    before = (home / "settings.json").read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsafe home reached identity, daemon startup or IPC")

    monkeypatch.setattr(boundary, "node_owner_or_none", forbidden)
    with pytest.raises(GuiError) as caught:
        admit_source(_environment(home), request_once=forbidden, ensure_daemon=forbidden)

    assert caught.value.code == "GUI_HOME_UNSAFE"
    assert caught.value.data == {"home": str(home.resolve())}
    assert "0700" in str(caught.value)
    assert home.stat().st_mode & 0o777 == mode
    assert (home / "settings.json").read_bytes() == before
    assert not (home / "state").exists()
    assert not (home / "secrets").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows access control")
def test_corrected_home_can_be_admitted_and_save_its_first_key(tmp_path: Path) -> None:
    from hyprial.identity import SecretResolver

    home = _home(tmp_path, owner=None)
    home.chmod(0o755)
    with pytest.raises(GuiError, match="0700"):
        admit_source(_environment(home))
    home.chmod(0o700)
    ping = {**_ping(home), "owner": None, "phase": "bootstrap"}
    admitted = admit_source(
        _environment(home), request_once=_request_once(ping, _catalog()),
    )
    assert admitted.phase == "bootstrap"
    path = SecretResolver(admitted.home, None).write_user_provider(
        "first-key", {"DEEPSEEK_API_KEY": "synthetic-test-value"},
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"DEEPSEEK_API_KEY": "synthetic-test-value"}


@pytest.mark.parametrize("failure", ["private-access", "io"])
def test_home_privacy_check_failure_is_sanitized_and_never_starts_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from hyprial.kernel import PrivateAccessUnsupported
    from hyprial.shell.impl.gui.backend import admission as boundary

    home = _home(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> None:
        if failure == "private-access":
            raise PrivateAccessUnsupported("private-internal-sentinel")
        raise OSError("private-internal-sentinel")

    monkeypatch.setattr(boundary, "secure_private_directory", refuse)
    with pytest.raises(GuiError) as caught:
        admit_source(_environment(home), ensure_daemon=lambda: pytest.fail("daemon started"))
    assert caught.value.code == "GUI_HOME_UNSAFE"
    assert "private-internal-sentinel" not in str(caught.value)
    assert not (home / "state").exists()


def test_daemon_admission_probes_an_ownerless_home_and_refuses_without_an_answer(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, owner=None)
    contacted: list[str] = []

    def request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        contacted.append(method)
        raise ipc_errors.DaemonUnavailableError("no daemon answers")

    with pytest.raises(GuiError) as caught:
        admit_source(_environment(home), request_once=request)

    assert caught.value.code == "GUI_DAEMON_UNAVAILABLE"
    assert contacted == ["ping"]


def test_daemon_admission_rejects_a_principal_on_an_ownerless_home(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, owner=None)
    environ = _environment(home)
    environ["HYPRIAL_GUI_PRINCIPAL"] = ACTOR
    contacted = False

    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal contacted
        contacted = True
        raise AssertionError("a refused bootstrap must not contact a daemon")

    with pytest.raises(GuiError) as caught:
        admit_source(environ, request_once=request)

    assert caught.value.code == "GUI_PRINCIPAL_MISMATCH"
    assert caught.value.data == {"provided": ACTOR}
    assert contacted is False


def test_daemon_admission_keeps_the_serving_phase_for_an_owned_home(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    admission = admit_source(
        _environment(home),
        request_once=_request_once(_ping(home), _catalog()),
    )

    assert admission.phase == "serving"
    assert admission.owner == OWNER
    assert admission.principal == ACTOR


def test_source_admission_never_carries_a_principal_without_an_owner() -> None:
    with pytest.raises(ValueError, match="ownerless"):
        SourceAdmission(
            driver="daemon",
            owner=None,
            node=NODE,
            principal=ACTOR,
            home=None,
            state_dir=None,
            socket_path=None,
            phase="bootstrap",
        )
    with pytest.raises(ValueError, match="requires an owner"):
        SourceAdmission(
            driver="daemon",
            owner=None,
            node=NODE,
            principal=None,
            home=None,
            state_dir=None,
            socket_path=None,
        )


def test_daemon_admission_rejects_socket_mismatch_before_contact(tmp_path: Path) -> None:
    home = _home(tmp_path)
    environ = _environment(home)
    environ["HARNESS_SOCKET_PATH"] = str(tmp_path / "other.sock")
    with pytest.raises(GuiError) as caught:
        admit_source(environ, request_once=lambda *args, **kwargs: {})
    assert caught.value.code == "GUI_SOCKET_MISMATCH"


def test_daemon_admission_rejects_explicit_principal_mismatch(tmp_path: Path) -> None:
    home = _home(tmp_path)
    environ = _environment(home)
    environ["HYPRIAL_GUI_PRINCIPAL"] = f"agent:{OWNER}:{NODE}:someone-else"
    with pytest.raises(GuiError) as caught:
        admit_source(
            environ,
            request_once=_request_once(_ping(home), _catalog()),
        )
    assert caught.value.code == "GUI_PRINCIPAL_MISMATCH"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "different-owner"),
        ("nodeId", "different-node"),
        ("socket", "/different/daemon.sock"),
    ],
)
def test_daemon_admission_rejects_observed_identity_mismatch(
    tmp_path: Path, field: str, value: str
) -> None:
    home = _home(tmp_path)
    ping = _ping(home)
    ping[field] = value
    with pytest.raises(GuiError) as caught:
        admit_source(
            _environment(home),
            request_once=_request_once(ping, _catalog()),
        )
    assert caught.value.code == "GUI_DAEMON_IDENTITY"


def test_daemon_admission_rejects_restoring_phase(tmp_path: Path) -> None:
    home = _home(tmp_path)
    ping = _ping(home)
    ping["phase"] = "restoring"
    ping["restorePending"] = True
    with pytest.raises(GuiError) as caught:
        admit_source(
            _environment(home),
            request_once=_request_once(ping, _catalog()),
        )
    assert caught.value.code == "GUI_DAEMON_NOT_READY"


def test_daemon_admission_rejects_unreachable_daemon(tmp_path: Path) -> None:
    home = _home(tmp_path)

    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ipc_errors.DaemonUnavailableError("not running")

    with pytest.raises(GuiError) as caught:
        admit_source(_environment(home), request_once=request)
    assert caught.value.code == "GUI_DAEMON_UNAVAILABLE"


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "other", "packageVersion": __version__, "methods": []},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "epoch-1", "packageVersion": "0.0.0", "methods": []},
    ],
)
def test_daemon_admission_rejects_catalog_failure(
    tmp_path: Path, catalog: dict[str, Any]
) -> None:
    home = _home(tmp_path)
    with pytest.raises(GuiError) as caught:
        admit_source(
            _environment(home),
            request_once=_request_once(_ping(home), catalog),
        )
    assert caught.value.code == "GUI_DAEMON_CATALOG"


@pytest.mark.parametrize(
    "name",
    [
        "HYPRIAL_BACKEND_ENDPOINT",
        "HYPRIAL_GUI_AUTH",
        "HYPRIAL_GUI_SETUP",
        "HYPRIAL_GUI_SETUP_CLI",
        "HYPRIAL_GUI_UPSTREAM_TOKEN",
        "HYPRIAL_GUI_LAUNCH_GRANT",
        "HYPRIAL_GUI_HANDOFF_PATH",
        "HYPRIAL_GUI_PUBLICATION_PATH",
    ],
)
def test_source_admission_rejects_legacy_or_preseeded_secrets(
    tmp_path: Path, name: str
) -> None:
    home = _home(tmp_path)
    environ = _environment(home)
    environ[name] = "forbidden"
    with pytest.raises(GuiError) as caught:
        admit_source(environ, request_once=lambda *args, **kwargs: {})
    assert caught.value.code == "GUI_CONFIG_CONFLICT"


def test_contract_mock_admission_needs_no_home_or_daemon(tmp_path: Path) -> None:
    admission = admit_source(
        {
            "HOME": str(tmp_path / "user"),
            "HYPRIAL_HOME": str(tmp_path / "missing-home"),
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        },
        request_once=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mock admission must not contact a daemon")
        ),
    )

    assert admission.driver == "contract-mock"
    assert admission.home is None
    assert admission.socket_path is None
    assert admission.principal == "user:contract-mock"


def test_missing_or_unknown_driver_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    for value in (None, "", "fixture", "live"):
        environ = {"HYPRIAL_HOME": str(home)}
        if value is not None:
            environ["HYPRIAL_GUI_BACKEND_DRIVER"] = value
        with pytest.raises(GuiError) as caught:
            admit_source(environ, request_once=lambda *args, **kwargs: {})
        assert caught.value.code == "GUI_BACKEND_DRIVER"


def test_daemon_admission_rejects_env_symlink_and_reparse_home_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel import link_directory, manysys
    from hyprial.shell.impl.gui.backend import admission as boundary

    real_home = _home(tmp_path)
    linked_home = tmp_path / "linked-home"
    link_directory(real_home, linked_home)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("linked or reparse home reached identity, daemon startup or IPC")

    monkeypatch.setattr(boundary, "node_owner_or_none", forbidden)

    linked_env = {
        **_environment(real_home),
        "HYPRIAL_HOME": str(linked_home),
        "HARNESS_STATE_DIR": str(linked_home / "state"),
    }
    with pytest.raises(GuiError) as caught:
        admit_source(linked_env, request_once=forbidden, ensure_daemon=forbidden)
    assert caught.value.code == "GUI_HOME_UNSAFE"
    assert caught.value.data == {"home": str(Path(os.path.abspath(linked_home)))}

    reparse_home = tmp_path / "reparse-home"
    reparse_home.mkdir(mode=0o700)
    (reparse_home / "settings.json").write_text('{"version": 1}\n', encoding="utf-8")
    reparse_ino = reparse_home.lstat().st_ino
    monkeypatch.setattr(manysys, "path_is_reparse", lambda info: info.st_ino == reparse_ino)

    reparse_env = {
        **_environment(reparse_home),
        "HYPRIAL_HOME": str(reparse_home),
        "HARNESS_STATE_DIR": str(reparse_home / "state"),
    }
    with pytest.raises(GuiError) as caught:
        admit_source(reparse_env, request_once=forbidden, ensure_daemon=forbidden)
    assert caught.value.code == "GUI_HOME_UNSAFE"
    assert caught.value.data == {"home": str(Path(os.path.abspath(reparse_home)))}


def test_daemon_admission_enforces_hard_gate_on_external_harness_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel import link_directory, manysys, posix_metadata_enforced
    from hyprial.shell.impl.gui.backend import admission as boundary

    home = _home(tmp_path)
    real_state = tmp_path / "real-external-state"
    real_state.mkdir(mode=0o700)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsafe external HARNESS_STATE_DIR reached identity or daemon effects")

    linked_state = tmp_path / "linked-external-state"
    link_directory(real_state, linked_state)
    with monkeypatch.context() as patched:
        patched.setattr(boundary, "node_owner_or_none", forbidden)
        with pytest.raises(GuiError) as caught:
            admit_source(
                {**_environment(home), "HARNESS_STATE_DIR": str(linked_state)},
                request_once=forbidden,
                ensure_daemon=forbidden,
            )
        assert caught.value.code == "GUI_HOME_UNSAFE"
        assert caught.value.data["state_dir"] == str(Path(os.path.abspath(linked_state)))

    with monkeypatch.context() as patched:
        patched.setattr(boundary, "node_owner_or_none", forbidden)
        with pytest.raises(GuiError) as caught:
            admit_source(
                {**_environment(home), "HARNESS_STATE_DIR": str(linked_state / "nested")},
                request_once=forbidden,
                ensure_daemon=forbidden,
            )
        assert caught.value.code == "GUI_HOME_UNSAFE"
        assert not (real_state / "nested").exists()

    file_state = tmp_path / "file-state"
    file_state.write_text("not a directory", encoding="utf-8")
    with monkeypatch.context() as patched:
        patched.setattr(boundary, "node_owner_or_none", forbidden)
        with pytest.raises(GuiError) as caught:
            admit_source(
                {**_environment(home), "HARNESS_STATE_DIR": str(file_state)},
                request_once=forbidden,
                ensure_daemon=forbidden,
            )
        assert caught.value.code == "GUI_HOME_UNSAFE"

    reparse_state = tmp_path / "reparse-external-state"
    reparse_state.mkdir(mode=0o700)
    reparse_ino = reparse_state.lstat().st_ino
    with monkeypatch.context() as patched:
        patched.setattr(boundary, "node_owner_or_none", forbidden)
        patched.setattr(manysys, "path_is_reparse", lambda info: info.st_ino == reparse_ino)
        with pytest.raises(GuiError) as caught:
            admit_source(
                {**_environment(home), "HARNESS_STATE_DIR": str(reparse_state)},
                request_once=forbidden,
                ensure_daemon=forbidden,
            )
        assert caught.value.code == "GUI_HOME_UNSAFE"
        assert caught.value.data["state_dir"] == str(Path(os.path.abspath(reparse_state)))

        with pytest.raises(GuiError) as caught:
            admit_source(
                {**_environment(home), "HARNESS_STATE_DIR": str(reparse_state / "missing")},
                request_once=forbidden,
                ensure_daemon=forbidden,
            )
        assert caught.value.code == "GUI_HOME_UNSAFE"

    if posix_metadata_enforced():
        open_state = tmp_path / "open-external-state"
        open_state.mkdir(mode=0o755)
        open_state.chmod(0o755)
        with monkeypatch.context() as patched:
            patched.setattr(boundary, "node_owner_or_none", forbidden)
            with pytest.raises(GuiError) as caught:
                admit_source(
                    {**_environment(home), "HARNESS_STATE_DIR": str(open_state)},
                    request_once=forbidden,
                    ensure_daemon=forbidden,
                )
            assert caught.value.code == "GUI_HOME_UNSAFE"
            assert open_state.stat().st_mode & 0o777 == 0o755

    for valid_state in (real_state, tmp_path / "absent-external-state"):
        ping = {**_ping(home), "socket": str(valid_state.resolve() / "daemon.sock")}
        admitted = admit_source(
            {**_environment(home), "HARNESS_STATE_DIR": str(valid_state)},
            request_once=_request_once(ping, _catalog()),
        )
        assert admitted.state_dir == valid_state.resolve()
