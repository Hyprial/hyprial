//! Build script of the `hyprial._core.actors` extension crate.
//!
//! Decisions:
//! - HQ review of round 1 (macOS arm64): the cdylib must link with
//!   `-undefined dynamic_lookup` on Darwin, because an extension module
//!   resolves the `Py*` symbols from the loading interpreter. Without it the
//!   unmodified `scripts/build/build_native_engine.sh --conformance` fails at
//!   the link step (rc=101, undefined `Py*` symbols). PyO3 0.30's documented
//!   mechanism is `pyo3_build_config::add_extension_module_link_args()`; it
//!   emits the two `cargo:rustc-cdylib-link-arg` lines for Darwin targets
//!   only and nothing on Linux, so the manylinux artefact is unchanged.
//! - Emitted unconditionally rather than behind the `extension-module`
//!   feature: the flags affect only the cdylib, and a Darwin cdylib that
//!   additionally links libpython (feature off, `cargo test`) still loads.
//!   No RUSTFLAGS or `.cargo/config.toml` is involved, so every cargo
//!   invocation of this crate gets the same link arguments.

fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
