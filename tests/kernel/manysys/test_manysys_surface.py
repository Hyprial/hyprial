# Decision record (test_manysys_surface, W1-MANYSYS 2026-11-21):
# - Verifies that hyprial.kernel exports manysys and every migrated/new OS
#   primitive with object-identity parity against hyprial.kernel.impl.manysys.interface.
"""Public surface and leaf-purity checks for ``hyprial.kernel.manysys``."""

from __future__ import annotations

import hyprial.kernel as kernel_module
from hyprial.kernel import interface as kernel_interface, manysys
import hyprial.kernel.impl.manysys.interface as manysys_interface


def test_kernel_exports_manysys_interface_module() -> None:
    assert "manysys" in kernel_interface.__all__
    assert "manysys" in kernel_module.__all__
    assert manysys is manysys_interface
    assert kernel_module.manysys is manysys_interface


def test_manysys_all_exports_exist_and_match_kernel_reexports() -> None:
    assert len(manysys.__all__) == len(set(manysys.__all__))
    for name in manysys.__all__:
        value = getattr(manysys, name)
        assert value is not None
        if name in kernel_module.__all__:
            assert getattr(kernel_module, name) is value
            assert getattr(kernel_interface, name) is value


def test_new_cross_platform_primitives_are_exported_on_kernel_and_manysys() -> None:
    expected = {
        "GUEST_WORKER_STATE",
        "StdinStopWatcher",
        "apply_private_umask",
        "darwin_process_start_time",
        "deliver_main_termination",
        "executable_filename",
        "file_modes_are_significant",
        "host_machine_identifier",
        "is_directory_link",
        "link_directory",
        "open_descriptor_count",
        "posix_runtime_bus_dir",
        "probe_process_group_or_pid",
        "process_cpu_sample",
        "process_parent_pid",
        "scoped_process_birth_marker",
        "spawn_detached_daemon",
        "terminate_fenced_process",
        "unlink_directory_link",
    }
    assert expected.issubset(set(manysys.__all__))
    assert expected.issubset(set(kernel_module.__all__))
