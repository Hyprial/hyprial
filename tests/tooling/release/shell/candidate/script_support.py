"""Run scripts/build/package-macos-shell.sh on Linux with fake host tools.

The script is the one place the macOS package is built, locally by a
developer and from the manual workflow. The tests either source it and call
a single stage (the way the workflow's step scripts used to be run), or run it
whole inside a throw-away repository whose PATH is built from scratch: fake
``uname``, ``sw_vers``, ``node``, ``npm``, ``uv``, ``cargo``, ``codesign``,
``hdiutil`` and a fake ``scripts/build/build-wheel.sh``, plus links to the few
real utilities the script calls, so the Node of the machine running the tests
never leaks in. Every fake appends its command line to ``FAKE_LOG`` and fails
when its key is listed in ``FAKE_FAIL``. It also appends the ``UV_PYTHON`` it
sees and every ``APPLE_*`` / ``TAURI_SIGNING_*`` variable visible to it to
``FAKE_LOG.env``; the fake ``uv`` answers the interpreter query like a uv whose
default is a free-threaded 3.14 unless ``UV_PYTHON`` is 3.12, and records the
``--root-parent`` it is given in ``FAKE_LOG.parents``. For the
``--developer-id`` mode the fake ``uv`` also stands in for
``packaging/clients/sign_bundle.py`` (``--check-only`` fails with key
``sign_preflight``, signing with ``sign`` after recording two notary ids,
``--verify-only`` with ``verify_signed``; signing writes a signed output
directory with SHA256SUMS like the real tool; ``--verify-only`` records the
SHA-256 of what it verified and of the dmg it "attached" and makes the
``--extract-to`` app; key ``verified_mismatch`` records a wrong dmg digest;
with ``--mark-unfinished`` signing writes ``UNFINISHED.txt`` into the signed
output and ``--verify-only`` into the ``--extract-to`` directory, as the real
tool does) and for ``scripts/ci/wheel_source_remote.py``
(key ``public_remote`` makes the wheel internal-only) and for
``packaging/clients/signing/archives.py`` (key ``archives``). The fake
``hdiutil info`` prints ``FAKE_HDIUTIL_INFO`` (``%b`` escapes) and every
``hdiutil <verb>`` fails with key ``hdiutil_<verb>``; the fake Tauri build
touches ``FAKE_HANG_BUILD`` and sleeps when it is set, and with
``FAKE_SOURCE_CHANGE`` ``dirty`` (an untracked file) or ``head`` (a new
commit) changes the checkout during the build; the fake smokes touch
``FAKE_HANG_SMOKE`` and sleep when it is set; key ``signed_build_info``
makes the fake signing write no ``BUILD-INFO.txt``; key ``build_info_readonly``
makes the fake wheel remote check replace ``dist/shell-macos/BUILD-INFO.txt``
with a directory (an append that fails for every user, root included).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.tooling.release.shell.support import ROOT

SCRIPT = ROOT / "scripts" / "build" / "package-macos-shell.sh"
SIGNED_COLLECT = ROOT / "scripts" / "build" / "macos-package" / "signed-collect.sh"
BASH = shutil.which("bash")
REAL_TOOLS = ("basename", "cat", "chmod", "cp", "dirname", "find", "git", "grep", "ls", "mkdir", "mktemp", "rm", "rmdir", "sed", "sleep", "touch", "tr", "wc", "bash")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

FAKE_PRELUDE = (
    'printf "%s %s\\n" "$(basename "$0")" "$*" >> "${FAKE_LOG:-/dev/null}"\n'
    'if [ -n "${FAKE_LOG:-}" ]; then printf "%s UV_PYTHON=%s visible=%s\\n" "$(basename "$0")" "${UV_PYTHON-unset}" "$(compgen -v | grep -E \'^(APPLE_|TAURI_SIGNING|HYPRIAL_SIGN_BUNDLE)\' | tr \'\\n\' , || true)" >> "${FAKE_LOG}.env"; fi\n'
    'fail() { case " ${FAKE_FAIL:-} " in *" $1 "*) echo "fake $1 failure"; exit 3 ;; esac; }\n'
)
FAKE_UV = FAKE_PRELUDE + r"""
if [ "$1" = --version ]; then
  echo "uv ${FAKE_UV_VERSION:-0.11.7} (a1b2c3d4e 2026-11-01 aarch64-apple-darwin)"
  exit 0
fi
case "$*" in
  *sys.executable*)
    if [ "${UV_PYTHON:-}" = 3.12 ]; then
      echo "${FAKE_PYTHON:-/fake/uv/python3.12 3.12.9 gil-disabled=0}"
    else
      echo "/fake/uv/python3.14t 3.14.0 gil-disabled=1"
    fi
    exit 0
    ;;
esac
prev=""; script=""; evidence=""; runtime=""; stop=""; out=""; extract=""; mode=sign; mark=""
for arg in "$@"; do
  case "${prev}" in
    --root-parent) echo "${arg}" >> "${FAKE_LOG}.parents" ;;
    --evidence) evidence="${arg}" ;;
    --runtime) runtime="${arg}" ;;
    --stop) stop="${arg}" ;;
    --out) out="${arg}" ;;
    --extract-to) extract="${arg}" ;;
  esac
  case "${arg}" in
    packaging/clients/*.py|scripts/ci/*.py) script="${arg##*/}" ;;
    --check-only) mode=check ;;
    --verify-only) mode=verify ;;
    --mark-unfinished) mark=1 ;;
  esac
  prev="${arg}"
done
case "${script}" in
  stage_runtime.py)
    fail stage
    mkdir -p clients/native/src-tauri/runtime
    echo '{"files": []}' > clients/native/src-tauri/runtime/runtime-manifest.json
    ;;
  verify_runtime.py)
    case "${runtime}" in *.app/*) fail verify_bundle ;; *) fail verify_staged ;; esac
    echo '{}' > "${evidence}"
    ;;
  smoke_bundle.py)
    if [ -n "${FAKE_HANG_SMOKE:-}" ]; then touch "${FAKE_HANG_SMOKE}"; sleep 60; fi
    fail "smoke_${stop}"
    echo '{}' > "${evidence}"
    ;;
  sign_bundle.py)
    case "${mode}" in
      check) fail sign_preflight; echo '{}' > "${evidence}" ;;
      verify)
        fail verify_signed
        dmg="$(cd "${out}" && ls *-signed.dmg)"
        sum() { shasum -a 256 "$1" | sed 's/ .*//'; }
        dmg_sum="$(sum "${out}/${dmg}")"
        case " ${FAKE_FAIL:-} " in *" verified_mismatch "*) dmg_sum="$(printf 'x' | shasum -a 256 | sed 's/ .*//')" ;; esac
        printf '{\n  "finalCopy": {\n    "app": "%s",\n    "dmgSha256": "%s"\n  },\n  "verifiedSha256": {\n    "%s": "%s",\n    "SHA256SUMS": "%s",\n    "runtime-manifest.json": "%s"\n  }\n}\n' \
          "${extract}/app/HyprialOS.app" "${dmg_sum}" "${dmg}" "$(sum "${out}/${dmg}")" "$(sum "${out}/SHA256SUMS")" "$(sum "${out}/runtime-manifest.json")" > "${evidence}"
        if [ -n "${mark}" ]; then mkdir -p "${extract}"; echo "NOT a release: unfinished (fake)" > "${extract}/UNFINISHED.txt"; fi
        mkdir -p "${extract}/app/HyprialOS.app/Contents"
        ;;
      sign)
        printf '{\n  "notary": [\n    {\n      "id": "11111111-2222-3333-4444-555555555555",\n      "label": "app"\n    },\n    {\n      "id": "66666666-7777-8888-9999-000000000000",\n      "label": "dmg"\n    }\n  ]\n}\n' > "${evidence}"
        fail sign
        mkdir -p "${out}/app/HyprialOS.app/Contents"
        printf 'signed dmg' > "${out}/HyprialOS_0.0.0_aarch64-signed.dmg"
        echo '{"files": {"signed": true}}' > "${out}/runtime-manifest.json"
        case " ${FAKE_FAIL:-} " in *" signed_build_info "*) ;; *) printf 'HyprialOS desktop bundle, Developer ID signed (not ad hoc), notarized and stapled\nteam id: ABCDE12345\n' > "${out}/BUILD-INFO.txt" ;; esac
        (cd "${out}" && shasum -a 256 HyprialOS_0.0.0_aarch64-signed.dmg runtime-manifest.json > SHA256SUMS)
        if [ -n "${mark}" ]; then echo "NOT a release: unfinished (fake)" > "${out}/UNFINISHED.txt"; fi
        ;;
    esac
    ;;
  wheel_source_remote.py)
    case " ${FAKE_FAIL:-} " in *" build_info_readonly "*) rm -f dist/shell-macos/BUILD-INFO.txt; mkdir dist/shell-macos/BUILD-INFO.txt ;; esac
    fail public_remote
    ;;
  archives.py)
    fail archives
    echo "archives: 0 zip-family, with Mach-O [], unreadable [], other kinds []"
    ;;
esac
"""
FAKE_CARGO = FAKE_PRELUDE + r"""
case "$1" in
  --version) echo "cargo ${FAKE_CARGO_VERSION:-1.93.1} (a1b2c3d4e 2026-01-01)" ;;
  install)
    fail tauri_cli
    prev=""; root=""
    for arg in "$@"; do
      if [ "${prev}" = --root ]; then root="${arg}"; fi
      prev="${arg}"
    done
    mkdir -p "${root}/bin"
    printf '#!%s\necho "tauri-cli ${FAKE_TAURI_VERSION:-2.12.2}"\n' "$(command -v bash)" > "${root}/bin/cargo-tauri"
    chmod +x "${root}/bin/cargo-tauri"
    echo "${root}" >> "${FAKE_LOG}.roots"
    ;;
  tauri)
    fail build_app
    if [ -n "${FAKE_HANG_BUILD:-}" ]; then touch "${FAKE_HANG_BUILD}"; sleep 60; fi
    echo "tauri build in ${PWD##*/} with CI=${CI:-}" >> "${FAKE_LOG}"
    case "${FAKE_SOURCE_CHANGE:-}" in
      dirty) echo changed > ../../../stray.txt ;;
      head) git -c user.name=t -c user.email=t@example.test commit -q --allow-empty -m moved ;;
    esac
    bundle=target/release/bundle
    mkdir -p "${bundle}/macos/HyprialOS.app/Contents/Resources/runtime" "${bundle}/dmg"
    if [ -z "${FAKE_NO_DMG:-}" ]; then printf 'dmg bytes' > "${bundle}/dmg/HyprialOS_0.0.0_aarch64.dmg"; fi
    ;;
esac
"""
FAKE_CODESIGN = FAKE_PRELUDE + r"""
fail codesign
if [ "$1" = -dv ]; then
  echo "Identifier=com.hyprial.hyprialos" >&2
  echo "Signature=adhoc" >&2
fi
"""
FAKE_UNAME = r"""
case "$1" in
  -s) echo "${FAKE_UNAME_S:-Darwin}" ;;
  -m) echo "${FAKE_UNAME_M:-arm64}" ;;
esac
"""
FAKE_WHEEL_BUILD = FAKE_PRELUDE + r"""
fail wheel
mkdir -p "$1"
printf 'wheel' > "$1/hyprial-0.0.0+gabcdef0-py3-none-macosx_13_0_arm64.whl"
if [ -n "${FAKE_TWO_WHEELS:-}" ]; then printf 'wheel' > "$1/hyprial-0.0.1-py3-none-macosx_13_0_arm64.whl"; fi
"""


def executable(path: Path, body: str) -> Path:
    """Write ``body`` as a bash script at ``path``, executable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def node_dir(path: Path, version: str, *, npm: bool = True) -> Path:
    """A bin directory with a fake ``node`` and, like a real keg, an ``npm`` that runs under the first ``node`` on PATH."""

    executable(path / "node", f'echo "{version}"')
    if npm:
        executable(
            path / "npm",
            'case "$*" in\n'
            '  "version --json") node_version="$(node --version)"; printf \'{\\n  "npm": "11.0.0",\\n  "node": "%s",\\n  "v8": "13.6"\\n}\\n\' "${node_version#v}" ;;\n'
            '  *) echo "11.0.0" ;;\n'
            "esac",
        )
    return path


def _shasum(tools: Path) -> None:
    real = shutil.which("shasum")
    if real is not None:
        (tools / "shasum").symlink_to(real)
        return
    sha256sum = shutil.which("sha256sum")
    assert sha256sum is not None, "shasum or sha256sum is needed to run the collect stage"
    executable(tools / "shasum", f'test "$1" = -a && test "$2" = 256 || exit 2\nshift 2\nexec {sha256sum} "$@"')


def tools(root: Path) -> Path:
    """Links to the real utilities the script calls and fakes for the host tools."""

    bin_dir = root / "tools"
    bin_dir.mkdir()
    for name in REAL_TOOLS:
        real = shutil.which(name)
        assert real is not None, f"{name} is needed to run the package script"
        (bin_dir / name).symlink_to(real)
    _shasum(bin_dir)
    executable(bin_dir / "uname", FAKE_UNAME)
    executable(bin_dir / "sw_vers", 'echo "ProductVersion: 15.0"')
    executable(bin_dir / "uv", FAKE_UV)
    executable(bin_dir / "cargo", FAKE_CARGO)
    executable(bin_dir / "codesign", FAKE_CODESIGN)
    executable(bin_dir / "hdiutil", FAKE_PRELUDE + 'if [ "$1" = info ]; then printf "%b" "${FAKE_HDIUTIL_INFO:-}"; fi\nfail "hdiutil_$1"\n')
    return bin_dir


def make_repo(root: Path) -> tuple[Path, str]:
    """A git repository holding a copy of the script (and the signed collection it sources) and a fake wheel build; returns it and its HEAD."""

    repo = root / "repo"
    (repo / "scripts" / "build").mkdir(parents=True)
    (repo / "clients" / "native" / "src-tauri").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "build" / SCRIPT.name)
    (repo / "scripts" / "build" / SIGNED_COLLECT.parent.name).mkdir()
    shutil.copy(SIGNED_COLLECT, repo / "scripts" / "build" / SIGNED_COLLECT.parent.name / SIGNED_COLLECT.name)
    executable(repo / "scripts" / "build" / "build-wheel.sh", FAKE_WHEEL_BUILD)
    (repo / ".gitignore").write_text("dist/\nclients/native/src-tauri/target/\nclients/native/src-tauri/runtime/\n", encoding="utf-8")
    env = {**os.environ, **GIT_ENV}
    for command in (["init", "-q", "."], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *command], cwd=repo, env=env, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()
    return repo, head


def run_script(repo: Path, args: list[str], path: list[Path], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the copy of the script inside ``repo`` the way the runner does (``bash -e -o pipefail``)."""

    assert BASH is not None
    scratch = repo.parent
    (scratch / "tmp").mkdir(exist_ok=True)
    log = scratch / "fake.log"
    log.touch()
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", str(repo / "scripts" / "build" / SCRIPT.name), *args],
        cwd=repo,
        env={
            "PATH": os.pathsep.join(str(item) for item in path),
            "HOME": str(scratch / "home"),
            "TMPDIR": str(scratch / "tmp"),
            "FAKE_LOG": str(log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            **(env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def fake_log(repo: Path) -> list[str]:
    """The command lines the fakes recorded, in order."""

    return (repo.parent / "fake.log").read_text(encoding="utf-8").splitlines()


def call(snippet: str, workspace: Path, path: list[Path], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source the script, point it at ``workspace`` and run ``snippet`` (stage functions) under ``bash -e -o pipefail``."""

    assert BASH is not None
    workspace.mkdir(parents=True, exist_ok=True)
    program = f'source "{SCRIPT}"\nROOT="{workspace}"\ninit_paths\nOUT="{workspace}/candidate"\nmkdir -p "${{WORK}}"\n{snippet}\n'
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", program],
        cwd=workspace,
        env={"PATH": os.pathsep.join(str(item) for item in path), "HOME": str(workspace), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
