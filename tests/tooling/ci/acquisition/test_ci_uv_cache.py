"""ci-uv-cache.sh: use the persistent volume only when it is proven mounted and writable."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/ci-uv-cache.sh"


def run(tmp_path, *, root, mounts, fallback=None):
    env = {k: v for k, v in os.environ.items() if k not in {"CI_CACHE_ROOT", "GITHUB_ENV"}}
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text("".join(f"dev {target} ext4 rw 0 0\n" for target in mounts))
    github_env = tmp_path / "github_env"
    env.update(CI_UV_CACHE_PROC_MOUNTS=str(proc_mounts), GITHUB_ENV=str(github_env))
    if root is not None:
        env["CI_CACHE_ROOT"] = str(root)
    fallback = fallback or tmp_path / "tmpfs-uv-cache"
    result = subprocess.run(["bash", str(SCRIPT), str(fallback)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("CI_UV_CACHE "))
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    exported = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    return fields, exported, result.stdout


def test_a_mounted_writable_volume_is_used_and_reports_existing_entries(tmp_path):
    volume = tmp_path / "hb-ci-cache"
    (volume / "uv-wheels" / "wheels-v5").mkdir(parents=True)
    fields, exported, out = run(tmp_path, root=volume, mounts=["/", str(volume)])
    assert fields["persistent"] == "yes" and fields["writable"] == "yes"
    assert fields["mount"] == str(volume)
    assert fields["entries_before"] == "1"  # the cross-job visibility witness
    assert exported["UV_CACHE_DIR"] == str(volume / "uv-wheels")
    assert exported["UV_LINK_MODE"] == "copy"
    assert "::warning::" not in out
    assert not list((volume / "uv-wheels").glob(".probe.*"))  # the write probe cleans up


def test_a_cache_root_inside_a_mounted_parent_counts_as_mounted(tmp_path):
    volume = tmp_path / "mnt"
    root = volume / "cache"
    root.mkdir(parents=True)
    fields, exported, _ = run(tmp_path, root=root, mounts=["/", str(volume)])
    assert fields["persistent"] == "yes" and fields["mount"] == str(volume)


def test_an_unmounted_directory_is_not_trusted_as_persistent(tmp_path):
    # A plain directory inside the container layer would cache nothing across jobs.
    root = tmp_path / "not-a-mount"
    root.mkdir()
    fields, exported, out = run(tmp_path, root=root, mounts=["/"])
    assert fields["persistent"] == "no" and fields["mount"] == "none"
    assert exported["UV_CACHE_DIR"] == str(tmp_path / "tmpfs-uv-cache")
    assert "::warning::uv cache is NOT persistent" in out


def test_a_missing_variable_falls_back_loudly(tmp_path):
    fields, exported, out = run(tmp_path, root=None, mounts=["/"])
    assert fields["persistent"] == "no"
    assert exported["UV_CACHE_DIR"] == str(tmp_path / "tmpfs-uv-cache")
    assert "::warning::" in out


def test_a_read_only_volume_falls_back(tmp_path):
    volume = tmp_path / "ro"
    (volume / "uv-wheels").mkdir(parents=True)
    (volume / "uv-wheels").chmod(0o555)
    try:
        if os.access(volume / "uv-wheels", os.W_OK):  # running as root ignores the mode
            return
        fields, exported, out = run(tmp_path, root=volume, mounts=["/", str(volume)])
        assert fields["persistent"] == "no" and fields["writable"] == "no"
        assert "::warning::" in out
    finally:
        (volume / "uv-wheels").chmod(0o755)


def test_the_wheel_cache_never_shares_the_uv_release_archive_directory(tmp_path):
    text = SCRIPT.read_text()
    assert '"$root/uv-wheels"' in text
    assert '"$root/uv"' not in text  # ci-ensure-uv.sh keeps uv release archives there
