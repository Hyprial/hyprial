"""The guided entry import-app-dev-id.sh, first runs, against fake tools.

Runs on every platform. Pins that one command with scripted answers walks
the whole procedure through the subcommands of setup-macos-signing-identity.sh
(the guide holds no key, keychain or Apple logic of its own), that pasted
base64 is checked before it is saved, that a failed import or download is
offered again instead of being reported as done, that quitting, end of input
and Ctrl-C stop with the "run it again" line, and that deleting the private
directory removes only the files the procedure put there.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.identity_fakes import BASH, fake_cert
from tests.tooling.release.shell.signing.wizard.wizard_support import AGAIN, EMAIL, NAME, TEAM, WIZARD, Wizard, combined, fresh, pasted


def _installed(host: Wizard) -> bool:
    return any(cert.get("key") == "wizardkey0001" for cert in host.model()["certs"]) and "wizardkey0001" in host.model()["keys"]


def test_one_command_walks_the_whole_procedure_with_a_certificate_path(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    result = host.wizard(fresh(host, str(certificate)))
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert text.endswith("Done.") or result.stdout.rstrip().endswith("Done.")
    assert _installed(host)
    assert host.cer.exists() and (host.dir / "devid.key").exists()
    assert "Developer ID Application" in text and "G2 Sub-CA" in text and "developer.apple.com" in text
    assert "scp <user>@<this-mac>:" in text
    assert "check: PASS" in text and "skipped: notarization-credentials" in text
    assert host.password not in result.stdout + result.stderr
    assert host.password not in "\n".join(" ".join(argv) for argv in (call["argv"] for call in host.calls()))
    assert host.tool_calls("xcrun") == []
    sign = next(argv for argv in host.tool_calls("codesign") if "--sign" in argv)
    assert sign[sign.index("--sign") + 1] == host.model()["certs"][0]["sha1"]


def test_paste_prints_the_csr_stores_credentials_and_deletes_only_its_files(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = ["", "", EMAIL, NAME, "y", "paste", *pasted(certificate), host.password, "y", "", EMAIL, "", "yes"]
    result = host.wizard(answers)
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "copy everything between these lines" in text and '"csr": true' in result.stdout
    assert f"suggests the Team ID {TEAM}" in text
    assert host.tool_calls("xcrun")[0] == ["notarytool", "store-credentials", "devid-notary", "--apple-id", EMAIL, "--team-id", TEAM]
    assert host.tool_calls("xcrun")[1] == ["notarytool", "history", "--keychain-profile", "devid-notary"]
    assert "this contacts Apple" in text
    assert "Deleted: devid.key devid.csr developerID_application.cer DeveloperIDG2CA.cer." in text
    assert not host.dir.exists()
    assert _installed(host)


def test_a_bad_paste_is_explained_and_asked_again(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    garbage = tmp_path / "garbage.txt"
    garbage.write_text("not a certificate at all", encoding="utf-8")
    answers = ["", "", EMAIL, NAME, "n", "paste", "!!! not base64 !!!", "", "paste", *pasted(garbage), "paste", *pasted(certificate), host.password, "n", ""]
    result = host.wizard(answers)
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "That is not base64 text" in text
    assert "it is not an X.509 certificate" in text
    assert _installed(host)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"key": "another-key"}, "its key does not match devid.key"),
        ({"subject": "CN=Apple Development: Example (ABCDE12345)/OU=ABCDE12345"}, 'not a "Developer ID Application" certificate'),
        ({"expired": True}, "it has expired"),
    ],
)
def test_an_unusable_certificate_is_refused_and_asked_again(tmp_path: Path, overrides: dict, reason: str) -> None:
    host = Wizard(tmp_path)
    wrong = host.certificate("wrong.cer", **overrides)
    good = host.certificate()
    answers = ["", "", EMAIL, NAME, "n", str(tmp_path / "missing.cer"), str(wrong), str(good), host.password, "n", ""]
    result = host.wizard(answers)
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "missing.cer does not exist yet" in text
    assert reason in text
    assert _installed(host)


def test_a_failed_import_is_offered_again_and_never_reported_as_done(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = ["", "", EMAIL, NAME, "n", str(certificate), "not-the-secret-123", "r", host.password, "n", ""]
    result = host.wizard(answers)
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "could not unlock" in text and "The import failed (exit 1)" in text
    assert "not-the-secret-123" not in text
    assert _installed(host)


def test_quitting_after_a_failed_import_reports_no_success(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    result = host.wizard(["", "", EMAIL, NAME, "n", str(certificate), "wrong password", "q"])
    assert result.returncode == 1
    text = combined(result)
    assert AGAIN in text and "Done." not in text and "check: PASS" not in text
    assert host.model()["keys"] == []


def test_a_failed_intermediate_download_takes_a_file_instead(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    forged = fake_cert(tmp_path / "forged.cer", key="forged", subject="CN=Developer ID Certification Authority/OU=G2/O=Apple Inc./C=US", sha256="0" * 64)
    answers = ["", "", EMAIL, NAME, "n", str(certificate), "retry", str(forged), str(host.intermediate), host.password, "n", ""]
    result = host.wizard(answers, extra={"FAKE_FAIL": "curl"})
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "The download failed" in text and "403" in text
    assert "SHA-256 does not match" in text
    assert (host.dir / "DeveloperIDG2CA.cer").exists()
    assert _installed(host)


def test_a_failed_intermediate_download_takes_a_paste(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = ["", "", EMAIL, NAME, "n", str(certificate), "paste", *pasted(host.intermediate), host.password, "n", ""]
    result = host.wizard(answers, extra={"FAKE_FAIL": "curl"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed(host)


def test_a_failed_notary_step_can_be_skipped(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = ["", "", EMAIL, NAME, "n", str(certificate), host.password, "y", "", EMAIL, "my.profile", "s", ""]
    result = host.wizard(answers, extra={"FAKE_FAIL": "notarytool"})
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "Storing the credentials failed" in text and "skipped: notarization-credentials" in text
    assert all("--profile" not in argv for argv in host.tool_calls("codesign"))
    assert [argv[1] for argv in host.tool_calls("xcrun")] == ["store-credentials"]


@pytest.mark.parametrize(("bad", "message"), [("abc", "A Team ID is 10 capital letters or digits."), ("abcde12345", "A Team ID is 10 capital letters or digits.")])
def test_team_id_apple_id_and_profile_are_validated(tmp_path: Path, bad: str, message: str) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = ["", "", "not-an-email", EMAIL, "", "A/B", NAME, "n", str(certificate), host.password, "y", bad, "", "nobody", EMAIL, "bad name", "", ""]
    result = host.wizard(answers)
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert message in text
    assert text.count("That does not look like an e-mail address.") == 2
    assert "must not be empty" in text
    assert "Use letters, digits, dot, underscore or dash." in text


def test_a_failed_check_offers_the_import_again_then_finishes_without_deleting(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    answers = fresh(host, str(certificate))[:-1] + ["r", host.password, "s"]
    result = host.wizard(answers, extra={"FAKE_FAIL": "codesign"})
    assert result.returncode == 1
    text = combined(result)
    assert "check: FAIL" in text and "Done." not in text
    assert text.count("non-interactive signing: FAIL") == 2
    assert "Type yes to delete" not in text and "running the import again needs devid.key" in text
    assert (host.dir / "devid.key").exists()


def test_the_paste_instructions_ask_for_wrapped_base64(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    result = host.wizard(["", "", EMAIL, NAME, "n", "q"])
    text = combined(result)
    assert "base64 -b 64 -i developerID_application.cer" in text
    assert "openssl base64 -in developerID_application.cer" in text
    assert "much over 1000 characters" in text


def test_deleting_keeps_files_the_procedure_did_not_create(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    host.make_key()
    (host.dir / "notes.txt").write_text("mine\n", encoding="utf-8")
    result = host.wizard(["", "", "n", str(certificate), host.password, "n", "yes"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(path.name for path in host.dir.iterdir()) == ["notes.txt"]
    assert "it still contains notes.txt" in combined(result)


@pytest.mark.parametrize("answer", ["", "no", "y", "YES"])
def test_anything_but_yes_keeps_the_private_directory(tmp_path: Path, answer: str) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    result = host.wizard(fresh(host, str(certificate), delete=answer))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (host.dir / "devid.key").exists()
    assert "Delete or move it yourself" in combined(result)


PROMPTS = {0: "continue", 1: "directory", 2: "e-mail", 3: "name", 4: "print csr", 5: "certificate", 7: "notary", 8: "delete"}


@pytest.mark.parametrize("index", sorted(PROMPTS))
def test_q_at_any_prompt_stops_without_success(tmp_path: Path, index: int) -> None:
    host = Wizard(tmp_path)
    answers = fresh(host, str(host.certificate()))[:index] + ["q"]
    result = host.wizard(answers)
    assert result.returncode == 1, PROMPTS[index]
    text = combined(result)
    assert "Stopped." in text and AGAIN in text and "Done." not in text


@pytest.mark.parametrize("index", sorted(PROMPTS))
def test_end_of_input_at_any_prompt_exits_1(tmp_path: Path, index: int) -> None:
    host = Wizard(tmp_path)
    answers = fresh(host, str(host.certificate()))[:index]
    result = host.wizard(answers)
    assert result.returncode == 1, PROMPTS[index]
    text = combined(result)
    assert "No more input." in text and AGAIN in text and "Done." not in text


def test_end_of_input_inside_a_paste_exits_1(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    result = host.wizard(["", "", EMAIL, NAME, "n", "paste", "QUJD"])
    assert result.returncode == 1 and "No more input." in combined(result)


def test_ctrl_c_at_a_prompt_exits_130_with_the_resume_line(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    seen, out, err = "", "", ""
    with subprocess.Popen([BASH, str(WIZARD)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=host.env, start_new_session=True) as process:
        try:
            assert process.stdout is not None
            deadline = time.monotonic() + 30
            while "Continue" not in seen and time.monotonic() < deadline:
                chunk = os.read(process.stdout.fileno(), 4096).decode()
                if not chunk:
                    break
                seen += chunk
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
    assert "Continue" in seen
    assert process.returncode == 130
    assert "interrupted; nothing more was done. Run it again to continue." in err
    assert "Done." not in seen + out


def test_help_unknown_option_and_non_macos(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    shown = host.wizard("", "--help")
    assert shown.returncode == 0 and "usage: ./scripts/build/import-app-dev-id.sh" in shown.stdout
    assert "locked in an SSH session" in " ".join(shown.stdout.split())
    assert host.wizard("", "--frobnicate").returncode == 2
    other = host.wizard([""], extra={"FAKE_UNAME": "Linux"})
    assert other.returncode == 2 and "macOS only" in other.stderr


def test_test_only_options_are_announced(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    result = host.wizard(["q"], "--allow-other-type")
    assert "test-only option --allow-other-type in use" in result.stderr
    assert "HYPRIAL_SIGNING_SETUP_TOOL_PATH" in result.stderr


def test_another_keychain_is_passed_to_every_step(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    separate = tmp_path / "separate.keychain-db"
    separate.write_text("fake keychain\n", encoding="utf-8")
    certificate = host.certificate()
    result = host.wizard(fresh(host, str(certificate), notary=["y", "", EMAIL, ""]), "--keychain", str(separate))
    assert result.returncode == 0, result.stdout + result.stderr
    for argv in (call["argv"] for call in host.calls() if call["tool"] == "security" and call["argv"][:1] in (["import"], ["find-identity"], ["find-certificate"])):
        assert argv[-1] == str(separate) or argv[argv.index("-k") + 1] == str(separate), argv
    assert host.tool_calls("xcrun")[0][-2:] == ["--keychain", str(separate)]
