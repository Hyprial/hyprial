"""import-app-dev-id.sh continues from what exists, without a state file.

Every run derives its progress from the private directory and the keychain
(``setup-macos-signing-identity.sh status``): a key skips key and CSR
generation, a matching certificate at the default path skips the certificate
prompt, an installed identity skips the import, a pinned intermediate on disk
skips the download. Fake tools; every platform.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.tooling.release.shell.signing.wizard.wizard_support import AGAIN, EMAIL, NAME, Wizard, combined


def test_an_existing_key_skips_key_and_csr_generation(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    host.make_key()
    before = (host.dir / "devid.key").read_text(encoding="utf-8")
    result = host.wizard(["", "", "n", str(host.certificate()), host.password, "n", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "devid.key already exists: skipping key and CSR generation" in combined(result)
    assert (host.dir / "devid.key").read_text(encoding="utf-8") == before
    assert [argv[0] for argv in host.tool_calls("openssl")].count("genrsa") == 1


def test_a_certificate_already_in_place_skips_the_certificate_prompt(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    host.make_key()
    host.cer.write_bytes(host.certificate().read_bytes())
    result = host.wizard(["", "", host.password, "n", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    text = combined(result)
    assert "it matches the key; using it" in text and "Get the certificate from Apple" not in text


def test_a_certificate_in_place_that_does_not_fit_is_explained(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    host.make_key()
    host.cer.write_bytes(host.certificate("other.cer", key="someone-else").read_bytes())
    good = host.certificate()
    result = host.wizard(["", "", "n", str(good), host.password, "n", ""])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot be used: it was not made from" in combined(result)
    assert host.cer.read_bytes() == good.read_bytes()


def test_a_second_run_after_success_skips_the_import(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    first = host.wizard(["", "", EMAIL, NAME, "n", str(certificate), host.password, "n", ""])
    assert first.returncode == 0, first.stdout + first.stderr
    imports = len([call for call in host.calls() if call["argv"][:1] == ["import"]])
    second = host.wizard(["", "", "n", ""])
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already in the keychain: skipping the import" in combined(second)
    assert len([call for call in host.calls() if call["argv"][:1] == ["import"]]) == imports
    assert len(host.model()["keys"]) == 1


def test_stopping_at_the_certificate_and_running_again_continues_there(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    first = host.wizard(["", "", EMAIL, NAME, "n", "q"])
    assert first.returncode == 1 and AGAIN in combined(first)
    assert (host.dir / "devid.csr").exists()
    assert not [call for call in host.calls() if call["argv"][:1] == ["import"]]
    second = host.wizard(["", "", "n", str(host.certificate()), host.password, "n", ""])
    assert second.returncode == 0, second.stdout + second.stderr
    text = combined(second)
    assert "skipping key and CSR generation" in text and "Your e-mail address" not in text


def test_a_locked_keychain_on_resume_is_unlocked_by_running_the_import_again(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    first = host.wizard(["", "", EMAIL, NAME, "n", str(certificate), host.password, "n", ""])
    assert first.returncode == 0, first.stdout + first.stderr
    model = host.model()
    model["locked"] = True
    (host.state / "keychain.json").write_text(json.dumps(model), encoding="utf-8")
    second = host.wizard(["", "", "n", "r", host.password, "yes"])
    assert second.returncode == 0, second.stdout + second.stderr
    text = combined(second)
    assert "skipping the import" in text
    assert "usually the locked keychain" in text
    assert "non-interactive signing: FAIL" in text and "non-interactive signing: PASS" in text
    assert "already installed" in text and len(host.model()["keys"]) == 1
    assert not host.dir.exists()


def test_a_saved_intermediate_is_reused_without_a_download(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    host.make_key()
    host.dir.joinpath("DeveloperIDG2CA.cer").write_bytes(host.intermediate.read_bytes())
    result = host.wizard(["", "", "n", str(host.certificate()), host.password, "n", ""], extra={"FAKE_FAIL": "curl"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Using the Apple intermediate already saved" in combined(result)
    assert host.tool_calls("curl") == []


def test_a_keychain_with_an_identity_asks_before_making_another_key(tmp_path: Path) -> None:
    host = Wizard(tmp_path)
    certificate = host.certificate()
    assert host.wizard(["", "", EMAIL, NAME, "n", str(certificate), host.password, "n", "yes"]).returncode == 0
    assert not host.dir.exists()
    result = host.wizard(["", "", "n"])
    assert result.returncode == 1
    text = combined(result)
    assert "already holds 1 Developer ID Application identity" in text and "Stopped." in text
    assert not (host.dir / "devid.key").exists()
