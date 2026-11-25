//! HyprialOS native shell: one window around `hyprial gui`, nothing else.
//!
//! Decisions (shell-6.md §B.3 run-time sequence, §D.3 6b-5; review findings
//! 3, 4, 5, 7, 8):
//! - Single instance: a second start focuses the existing window and exits.
//! - The window opens on the bundled status page. The launcher command comes
//!   from `hyprial_shell_launcher::resolve_command` (`HYPRIAL_SHELL_LAUNCHER`,
//!   else `<resources>/runtime/bin/python3.12 -I -m hyprial.cli`); when neither
//!   exists the status page says so. stderr goes to `<app log dir>/launcher.log`.
//! - The child runs on the app's own hyprial home: `app_environment` keeps an
//!   explicit `HYPRIAL_HOME` from the app's environment, otherwise sets
//!   `<user home>/.hyprialos` (created 0700 when missing) and drops
//!   `HARNESS_STATE_DIR` (decision and
//!   socket-length reasoning in `launcher/src/home.rs`). The user home comes
//!   from Tauri's path resolver. The bundled runtime is
//!   `Contents/Resources/runtime` (`bundle.resources` from
//!   `packaging/clients/tauri.bundle.json`, given only at package time).
//! - Spawning, the startup read and the session watch run on one background
//!   thread. A failed start has already stopped the child (`start`); a failed
//!   navigation stops it here. Any failure, and a child exit after startup,
//!   navigates to the status page with `#error=…`. The grant only reaches
//!   `navigate`.
//! - Guards: main frame = the record's origin or the status page; new windows
//!   with an `http(s)` URL of another origin open in the system browser, all
//!   other new windows are denied. No IPC command, no capability, no plugin
//!   configuration; `withGlobalTauri` is false.
//! - Quit or last window closed: exit is prevented once, the planned stop
//!   (close stdin, 8 s, SIGTERM to the group, 7 s, SIGKILL) runs on a separate
//!   thread, then the app exits. `RunEvent::Exit` without that stop (an exit
//!   path that skipped `ExitRequested`) runs the same stop synchronously.
//! - SIGTERM or SIGKILL of the shell itself is not handled: the child sees EOF
//!   on its stdin pipe and stops by `--stdin-stop`.

use hyprial_shell_launcher::{
    allowed_main_frame, app_environment, ensure_app_home, external_target, resolve_command, spawn,
    start, status_page_url, ChildProcess, LauncherError, StopBudget, STARTUP_TIMEOUT,
};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};

const STATUS_PAGE: &str = "tauri://localhost/index.html";
const WINDOW: &str = "main";

#[derive(Default)]
struct Shell {
    child: Mutex<Option<ChildProcess>>,
    origin: Mutex<Option<Url>>,
    stopping: AtomicBool,
    stopped: AtomicBool,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn status_page() -> Url {
    Url::parse(STATUS_PAGE).expect("static status page URL")
}

fn show_error(app: &AppHandle, message: &str) {
    if let Some(window) = app.get_webview_window(WINDOW) {
        let _ = window.navigate(status_page_url(&status_page(), Some(message)));
    }
}

fn launcher_log(app: &AppHandle) -> Option<PathBuf> {
    let dir = app.path().app_log_dir().ok()?;
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir.join("launcher.log"))
}

fn run_launcher(app: AppHandle, shell: Arc<Shell>) {
    let resources = app.path().resource_dir().ok();
    let launcher_override = std::env::var_os("HYPRIAL_SHELL_LAUNCHER");
    let explicit_home = std::env::var_os("HYPRIAL_HOME");
    let user_home = app.path().home_dir().ok();
    let log = launcher_log(&app);
    let process = {
        let mut slot = lock(&shell.child);
        if shell.stopping.load(Ordering::SeqCst) {
            return;
        }
        let spawned = resolve_command(launcher_override.as_deref(), resources.as_deref())
            .map_err(LauncherError::from)
            .and_then(|command| {
                let changes = app_environment(explicit_home.as_deref(), user_home.as_deref())?;
                ensure_app_home(&changes)?;
                Ok(command.with_environment(changes))
            })
            .and_then(|command| spawn(&command, log.as_deref()));
        match spawned {
            Ok(process) => {
                *slot = Some(process.clone());
                process
            }
            Err(error) => return show_error(&app, &error.to_string()),
        }
    };
    let session = match start(&process, STARTUP_TIMEOUT, StopBudget::PLANNED) {
        Ok(session) => session,
        Err(error) => {
            if !shell.stopping.load(Ordering::SeqCst) {
                show_error(&app, &error.to_string());
            }
            return;
        }
    };
    *lock(&shell.origin) = Some(session.origin.clone());
    let navigated = Url::parse(session.launch_url.as_str())
        .ok()
        .and_then(|url| app.get_webview_window(WINDOW).map(|window| (window, url)))
        .is_some_and(|(window, url)| window.navigate(url).is_ok());
    if !navigated {
        let _ = process.stop();
        return show_error(&app, "the GUI page could not be opened in the window");
    }
    let end = process.wait_session_end();
    if shell.stopping.load(Ordering::SeqCst) {
        return;
    }
    let _ = process.stop();
    match end {
        Ok(end) => show_error(&app, &end.to_string()),
        Err(error) => show_error(&app, &error.to_string()),
    }
}

fn stop_child(shell: &Shell) {
    shell.stopping.store(true, Ordering::SeqCst);
    let process = lock(&shell.child).clone();
    if let Some(process) = process {
        let _ = process.stop();
    }
    shell.stopped.store(true, Ordering::SeqCst);
}

fn on_event(app: &AppHandle, event: RunEvent, shell: &Arc<Shell>) {
    match event {
        RunEvent::ExitRequested { api, .. } => {
            if shell.stopped.load(Ordering::SeqCst) {
                return;
            }
            api.prevent_exit();
            if !shell.stopping.swap(true, Ordering::SeqCst) {
                let app = app.clone();
                let shell = Arc::clone(shell);
                thread::spawn(move || {
                    stop_child(&shell);
                    app.exit(0);
                });
            }
        }
        RunEvent::Exit if !shell.stopped.load(Ordering::SeqCst) => stop_child(shell),
        _ => {}
    }
}

fn main() {
    let shell = Arc::new(Shell::default());
    let setup_shell = Arc::clone(&shell);
    let built = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window(WINDOW) {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(move |app| {
            let status = status_page();
            let navigation_shell = Arc::clone(&setup_shell);
            let window_shell = Arc::clone(&setup_shell);
            WebviewWindowBuilder::new(app, WINDOW, WebviewUrl::App("index.html".into()))
                .title("HyprialOS")
                .inner_size(1180.0, 820.0)
                .on_navigation(move |candidate| {
                    let origin = lock(&navigation_shell.origin).clone();
                    allowed_main_frame(candidate, origin.as_ref(), &status)
                })
                .on_new_window(move |candidate, _| {
                    let origin = lock(&window_shell.origin).clone();
                    if let Some(target) = external_target(&candidate, origin.as_ref()) {
                        let _ = open::that_detached(target.as_str());
                    }
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;
            let handle = app.handle().clone();
            let launcher_shell = Arc::clone(&setup_shell);
            thread::spawn(move || run_launcher(handle, launcher_shell));
            Ok(())
        })
        .build(tauri::generate_context!());
    match built {
        Ok(app) => app.run(move |handle, event| on_event(handle, event, &shell)),
        Err(error) => eprintln!("HyprialOS failed to start: {error}"),
    }
}
