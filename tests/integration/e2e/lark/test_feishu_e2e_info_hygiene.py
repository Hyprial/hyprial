from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[4]
RUNNER_PATH = ROOT / "contract" / "e2e" / "run-feishu-roundtrip.py"
E2E013_RUNNER_PATH = (
    ROOT / "contract" / "e2e" / "run-all-harness-lifecycle.py"
)


def load_runner(
    path: Path = RUNNER_PATH, name: str = "feishu_e2e_runner"
):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def test_reauth_preflight_helpers_use_adapter_user_auth_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contract.e2e._harness_lifecycle import lark_auth

    scenario_root = ROOT / "contract" / "e2e"
    sys.path.insert(0, str(scenario_root))
    try:
        from feishu import identity
    finally:
        sys.path.remove(str(scenario_root))

    executable = "/tmp/hyprial-under-test"
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout='{"ok":true,"status":"ready"}', stderr=""
        )

    monkeypatch.setenv("HYPRIAL_BIN", executable)
    monkeypatch.setattr(identity.subprocess, "run", run)
    assert identity._lark_reauth_check_summary(
        _ports=SimpleNamespace(entrypoint=ROOT / "contract" / "e2e" / "runner.py")
    ) == "adapter user-auth check status=ready"

    entry = {
        "os": SimpleNamespace(environ={"HYPRIAL_BIN": executable}),
        "Path": Path,
        "__file__": str(ROOT / "contract" / "e2e" / "runner.py"),
        "subprocess": SimpleNamespace(run=run),
        "json": json,
    }
    assert lark_auth._lark_reauth_check_summary(entry) == (
        "adapter user-auth check status=ready"
    )
    assert calls == [
        [executable, "adapter", "user-auth", "check", "--json"],
        [executable, "adapter", "user-auth", "check", "--json"],
    ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"content": '{"image_key":"img_json"}'}, {"image_key": "img_json"}),
        ({"content": "[Image: img_rendered]"}, {"image_key": "img_rendered"}),
        ({"content": "![Image](img_markdown)"}, {"image_key": "img_markdown"}),
        (
            {"content": '<file key="file_rendered" name="proof&amp;notes.txt"/>'},
            {"file_key": "file_rendered", "file_name": "proof&notes.txt"},
        ),
    ],
)
def test_live_runners_parse_json_and_rendered_lark_resource_content(
    message: dict[str, str], expected: dict[str, str]
) -> None:
    runners = (
        load_runner(),
        load_runner(E2E013_RUNNER_PATH, "e2e013_runner"),
    )

    for runner in runners:
        assert runner._lark_message_content(message) == expected


def test_live_runners_reject_unknown_rendered_resource_content() -> None:
    runners = (
        load_runner(),
        load_runner(E2E013_RUNNER_PATH, "e2e013_runner_invalid"),
    )

    for runner in runners:
        with pytest.raises(AssertionError, match="supported rendered resource"):
            runner._lark_message_content({"content": "[Sticker]"})


def test_e2e013_download_verifier_uses_the_source_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = load_runner(E2E013_RUNNER_PATH, "e2e013_runner_download")
    source = tmp_path / "source.png"
    source.write_bytes(b"png-content")
    observed: list[str] = []

    def lark_cli(*arguments: str, cwd: Path | None = None):  # type: ignore[no-untyped-def]
        assert cwd == tmp_path
        output = arguments[arguments.index("--output") + 1]
        observed.append(output)
        (cwd / output).write_bytes(source.read_bytes())
        return {"ok": True}

    monkeypatch.setattr(runner, "lark_cli", lark_cli)

    assert runner.assert_downloaded_resource(
        tmp_path,
        message={"message_id": "om_test"},
        resource_key="img_test",
        kind="image",
        source=source,
    ) == "om_test"
    assert observed == ["user-downloaded-agent-image.png"]


def test_e2e013_transcript_route_evidence_requires_all_three_fields(
    tmp_path: Path,
) -> None:
    runner = load_runner(E2E013_RUNNER_PATH, "e2e013_runner_transcript")
    source = tmp_path / "source.png"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        f'{{"path":"{source}","resourceDeliveries":[]}}\n', encoding="utf-8"
    )

    assert not runner.transcript_has_route_delivery(
        transcript, source=source, native_message_id="om_expected"
    )

    transcript.write_text(
        f'{{"path":"{source}","resourceDeliveries":'
        '[{"nativeMessageId":"om_expected"}]}\n',
        encoding="utf-8",
    )
    assert runner.transcript_has_route_delivery(
        transcript, source=source, native_message_id="om_expected"
    )


def test_e2e013_image_lookup_skips_stale_identical_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = load_runner(E2E013_RUNNER_PATH, "e2e013_runner_image_selection")
    source = tmp_path / "source.png"
    transcript = tmp_path / "session.jsonl"
    source.write_bytes(b"same-bytes-every-run")
    transcript.write_text(
        f'{{"path":"{source}","resourceDeliveries":'
        '[{"nativeMessageId":"om_current"}]}\n',
        encoding="utf-8",
    )

    def lark_cli(*arguments: str, cwd: Path | None = None):  # type: ignore[no-untyped-def]
        assert cwd is None
        if arguments[:2] == ("im", "+chat-messages-list"):
            return {
                "data": {
                    "messages": [
                        {
                            "message_id": "om_stale",
                            "create_time": "2026-08-16 15:07",
                            "sender": {"sender_type": "app"},
                        },
                        {
                            "message_id": "om_current",
                            "create_time": "2026-08-16 15:07",
                            "sender": {"sender_type": "app"},
                        },
                    ]
                }
            }
        assert arguments[:2] == ("im", "+messages-mget")
        message_id = arguments[arguments.index("--message-ids") + 1]
        return {
            "data": {
                "messages": [
                    {
                        "message_id": message_id,
                        "msg_type": "image",
                        "content": f"[Image: img_{message_id}]",
                    }
                ]
            }
        }

    monkeypatch.setattr(runner, "lark_cli", lark_cli)

    found = runner.bot_resource_message(
        "oc_test",
        "image",
        1,
        route_transcript=transcript,
        route_source=source,
    )

    assert found is not None
    message, resource_key = found
    assert message["message_id"] == "om_current"
    assert resource_key == "img_om_current"


def test_shared_chat_resolves_by_route_name_not_route_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_runner()
    monkeypatch.delenv(runner.CHAT_ID_ENV_VAR, raising=False)
    gateway = {
        "routes": [
            {"name": "developer-dm", "type": "direct", "nativeId": "oc_private"},
            {"name": "py-test", "type": "direct", "nativeId": "oc_shared"},
        ]
    }

    assert runner.resolve_chat_id(gateway) == "oc_shared"


def test_reporter_errors_and_evidence_redact_native_identity_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = load_runner()
    chat_id = "oc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    open_user_id = "ou_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    evidence_path = tmp_path / "evidence.ndjson"

    runner.EvidenceLog(evidence_path).emit(
        "sdk.event",
        chat_id=chat_id,
        nested={"sender": open_user_id, "conversation": f"lark:{chat_id}:{chat_id}"},
    )
    runner.Reporter().fail(
        "A1-preflight", f"chat {chat_id} is inaccessible to {open_user_id}"
    )
    safe_error = runner.safe_cli_error(
        '{"error":{"message":"chat '
        + chat_id
        + " denied for "
        + open_user_id
        + '"}}',
        "",
    )

    combined = evidence_path.read_text(encoding="utf-8") + capsys.readouterr().out
    combined += safe_error
    assert chat_id not in combined
    assert open_user_id not in combined
    assert f"chat#{runner.text_digest(chat_id)}" in combined
    assert f"user#{runner.text_digest(open_user_id)}" in combined


def test_a10_rejects_raw_sensitive_ids() -> None:
    runner = load_runner()
    chat_id = "oc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    open_user_id = "ou_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    runner.assert_sanitized_evidence("safe", forbidden_values=(chat_id, open_user_id))
    with pytest.raises(runner.ContractFailure, match="sensitive native identifier"):
        runner.assert_sanitized_evidence(
            f"unsafe {chat_id}", forbidden_values=(chat_id, open_user_id)
        )


def test_committed_guidance_has_no_raw_chat_id_or_person_specific_path() -> None:
    paths = (
        ROOT / ".agents" / "skills" / "hyprial-e2e" / "SKILL.md",
        ROOT / "contract" / "e2e" / "04-feishu-roundtrip.md",
        ROOT / "contract" / "e2e" / "06-resident-providers.md",
        ROOT / "contract" / "e2e" / "feishu-live-roundtrip-evidence.md",
    )
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert re.search(r"\boc_[0-9a-f]{20,}\b", content) is None
    assert re.search(r"/Users/[^/\s]+/", content) is None
