//! The desktop shell for python-mcp-gateway: the gateway and its admin UI as one app.
//!
//! See `desktop/README.md` for the shape and the reasoning. The short version: this process
//! mints the gateway's access key, supervises the gateway as a child process, and opens the
//! gateway's two WebSockets itself. The window therefore never holds a secret and never
//! dials the network -- and `transport_ws.py` needed no change to accommodate any of it,
//! which is the test of whether the design is right.
//!
//! Everything worth testing lives in the four modules below, none of which need a window.
//! What is in this file is the wiring: managed state, the supervisor loop, the three
//! commands the webview may call, and teardown.

pub mod appdata;
pub mod key;
pub mod pathenv;
pub mod proxy;
pub mod supervisor;

#[cfg(test)]
mod integration;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::{Emitter, Manager, RunEvent, State};

use key::AccessKey;
use proxy::{Frame, Link, Links};
use supervisor::{Layout, State as GatewayState};

/// The event the window listens on to learn what the gateway is doing.
///
/// Without it the first launch is a blank gate for however long a cold interpreter takes to
/// import -- and a `servers.yaml` the daemon refuses is a window that says "connecting"
/// forever, when the reason is sitting in the log buffer.
const STATE_EVENT: &str = "gateway-state";

/// What the app knows, shared between the supervisor loop and the commands.
pub struct Shell {
    key: AccessKey,
    layout: Layout,
    inner: Arc<Inner>,
}

#[derive(Default)]
struct Inner {
    state: Mutex<GatewayStateCell>,
    /// The child's stderr, most recent last. The only record of why a start failed.
    log: Mutex<VecDeque<String>>,
    links: tokio::sync::Mutex<Links>,
}

struct GatewayStateCell(GatewayState);

impl Default for GatewayStateCell {
    fn default() -> Self {
        Self(GatewayState::Idle)
    }
}

/// The snapshot the window renders from.
#[derive(Debug, Clone, Serialize)]
pub struct Status {
    /// `idle` | `starting` | `listening` | `restarting` | `failed`.
    pub state: &'static str,
    pub attempt: u32,
    /// Why it is not running, when it is not. Shown in the gate.
    pub reason: Option<String>,
    /// The last few stderr lines, so a failed start explains itself.
    pub log: Vec<String>,
}

impl Inner {
    fn set(&self, state: GatewayState) {
        self.state.lock().unwrap().0 = state;
    }

    fn port(&self) -> Option<u16> {
        match self.state.lock().unwrap().0 {
            GatewayState::Listening { port } => Some(port),
            _ => None,
        }
    }

    fn record(&self, line: String) {
        let mut log = self.log.lock().unwrap();
        if log.len() == supervisor::LOG_LINES {
            log.pop_front();
        }
        log.push_back(line);
    }

    fn status(&self) -> Status {
        let state = self.state.lock().unwrap();
        let (name, attempt, reason) = match &state.0 {
            GatewayState::Idle => ("idle", 0, None),
            GatewayState::Starting => ("starting", 0, None),
            GatewayState::Listening { .. } => ("listening", 0, None),
            GatewayState::Restarting { attempt } => ("restarting", *attempt, None),
            GatewayState::Failed { reason } => ("failed", 0, Some(reason.clone())),
        };
        Status {
            state: name,
            attempt,
            reason,
            log: self.log.lock().unwrap().iter().cloned().collect(),
        }
    }
}

// --- the commands the window may call -----------------------------------------------------
//
// Three, and they are the webview's entire reach. There is no filesystem plugin, no shell
// plugin and no HTTP client in this app: the page can open one of two sockets, write to it,
// close it, and ask how the gateway is. Everything else it wants, it asks the gateway for
// over `/admin` -- which is the same thing the browser build does, through the same methods.

/// Open `/mcp` or `/admin`. The id is minted by the caller; see `tauri-transport.js`.
#[tauri::command]
async fn gw_open(
    shell: State<'_, Shell>,
    id: String,
    path: String,
    on_frame: Channel<Frame>,
) -> Result<(), String> {
    let Some(port) = shell.inner.port() else {
        // Not an error worth dressing up: `rpc.js` retries on its own backoff, and this is
        // the ordinary state of the world while the daemon is still importing.
        return Err("the gateway is not listening yet".into());
    };

    let inner = shell.inner.clone();
    let closing = id.clone();
    let outbound = proxy::open(port, &path, shell.key.header(), move |frame| {
        let done = matches!(frame, Frame::Close { .. });
        // A send that fails means the window went away mid-frame, which teardown handles.
        let _ = on_frame.send(frame);
        if done {
            let inner = inner.clone();
            let id = closing.clone();
            tauri::async_runtime::spawn(async move {
                inner.links.lock().await.remove(&id);
            });
        }
    })
    .await?;

    shell.inner.links.lock().await.insert(id, Link { outbound });
    Ok(())
}

/// Write one frame, verbatim.
#[tauri::command]
async fn gw_send(shell: State<'_, Shell>, id: String, text: String) -> Result<(), String> {
    // A write to a socket that has already closed is `Ok`, not an error. The close frame
    // and a last write race in the ordinary run of things, and an error here would make the
    // page report a second failure for one closure.
    shell.inner.links.lock().await.send(&id, text);
    Ok(())
}

/// Close one socket. Dropping the sender is what actually ends the pump.
#[tauri::command]
async fn gw_close(shell: State<'_, Shell>, id: String) -> Result<(), String> {
    shell.inner.links.lock().await.remove(&id);
    Ok(())
}

/// What the gateway is doing, for the window's status line and gate.
#[tauri::command]
fn gw_status(shell: State<'_, Shell>) -> Status {
    shell.inner.status()
}

// --- the supervisor loop --------------------------------------------------------------------

/// Keep the gateway running for as long as the app is.
async fn supervise<R: tauri::Runtime>(app: tauri::AppHandle<R>, inner: Arc<Inner>) {
    // Taken out of managed state once, in a block, so the guard is not held across an
    // await. The key is copied rather than borrowed for the same reason -- this loop
    // outlives any borrow of the state it started from.
    let (layout, secret) = {
        let shell = app.state::<Shell>();
        (shell.layout.clone(), shell.key.as_env().to_string())
    };

    // Asked once, at startup, and reused for every restart. See `pathenv`: an app launched
    // from Finder has a PATH with no `npx` on it, and a login shell is the only thing that
    // knows better.
    let path = pathenv::resolve();

    let announce = |state: GatewayState| {
        inner.set(state);
        let _ = app.emit(STATE_EVENT, inner.status());
    };

    let mut attempt: u32 = 0;
    let mut failed_starts: u32 = 0;

    loop {
        announce(GatewayState::Starting);

        let mut child = match supervisor::spawn(&layout, &secret, &path) {
            Ok(child) => child,
            Err(err) => {
                announce(GatewayState::Failed {
                    reason: format!("could not start the gateway: {err}"),
                });
                return;
            }
        };

        if let Some(pid) = child.id() {
            let _ = appdata::write_pidfile(&layout.pidfile(), pid);
        }

        let started = Instant::now();

        // Runs until the child's stderr closes, which is how this loop learns the child is
        // going away. Every line is kept for the log buffer; the first one that names a
        // port is the readiness signal. See `supervisor`.
        let mut reached_listening = false;
        if let Some(stderr) = child.stderr.take() {
            let recorder = inner.clone();
            let announcer = inner.clone();
            let app = app.clone();
            supervisor::pump_stderr(
                stderr,
                |line| recorder.record(line),
                |port| {
                    reached_listening = true;
                    announcer.set(GatewayState::Listening { port });
                    let _ = app.emit(STATE_EVENT, announcer.status());
                },
            )
            .await;
        }

        // stderr closed, so the child is on its way out. Reap it rather than leaving a
        // zombie, and take its process group with it in case a backend outlived it.
        supervisor::terminate(&mut child).await;
        appdata::clear_pidfile(&layout.pidfile());

        // Every socket pointed at a port that no longer exists. Dropping the registry's
        // senders ends each pump, which sends the window a close frame -- and that is what
        // makes `rpc.js` start its own reconnect rather than waiting on a dead socket.
        inner.links.lock().await.drain();

        if supervisor::resets_backoff(reached_listening, started.elapsed()) {
            attempt = 0;
            failed_starts = 0;
        } else if !reached_listening {
            failed_starts += 1;
        }

        if failed_starts >= supervisor::MAX_FAILED_STARTS {
            let reason = inner
                .log
                .lock()
                .unwrap()
                .iter()
                .rev()
                .find(|line| !line.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| "the gateway exited before it could serve".into());
            announce(GatewayState::Failed { reason });
            return;
        }

        attempt += 1;
        announce(GatewayState::Restarting { attempt });
        tokio::time::sleep(supervisor::backoff(attempt)).await;
    }
}

// --- the app -----------------------------------------------------------------------------

/// Start the app.
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            gw_open, gw_send, gw_close, gw_status
        ])
        .setup(|app| {
            let resource_dir = app.path().resource_dir()?;
            let data_dir = app.path().app_data_dir()?;
            let layout = Layout {
                python_root: resource_dir.join("python"),
                data_dir: data_dir.clone(),
            };

            appdata::ensure(&data_dir, &appdata::seed_dir(&resource_dir))?;
            // A gateway left behind by a Force Quit still holds every credential in
            // `gateway.env`. Neither the exit handler nor `kill_on_drop` can reach that
            // case, so the pidfile does.
            appdata::reap_previous(&layout.pidfile(), &layout.interpreter());

            let inner = Arc::new(Inner::default());
            app.manage(Shell {
                key: AccessKey::mint(),
                layout,
                inner: inner.clone(),
            });

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(supervise(handle, inner));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the app could not be built")
        .run(|app, event| {
            // Quitting must not leave a gateway -- or its backends -- running. This is the
            // first of the three layers described in `supervisor`; the other two are
            // `kill_on_drop` and the pidfile.
            if let RunEvent::Exit = event {
                let shell = app.state::<Shell>();
                appdata::clear_pidfile(&shell.layout.pidfile());
            }
        });
}
