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
pub mod panelframe;
pub mod pathenv;
pub mod proxy;
pub mod session;
pub mod settings;
pub mod supervisor;
pub mod tunnel;

#[cfg(test)]
mod integration;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use tauri::ipc::Channel;
use tauri::{Emitter, Manager, RunEvent, State};

use key::AccessKey;
use proxy::{Frame, Link, Links};
use session::{Phase, Status};
use settings::{Connection, Mode};
use supervisor::Layout;

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

struct Inner {
    phase: Mutex<Phase>,
    /// The running child's stderr, most recent last -- the daemon's, or `ssh`'s. The only
    /// record of why a start failed, and the same buffer for both because the window has one
    /// log pane and a person has one question.
    log: Mutex<VecDeque<String>>,
    links: tokio::sync::Mutex<Links>,
    /// Which gateway this window drives. Written by `conn_save`, read by the session loop
    /// at the top of every start.
    connection: Mutex<Connection>,
    /// Bumped to ask the session loop to tear down what it is running and start again from
    /// the settings. A `watch` rather than a flag because the loop has to be interruptible
    /// *while awaiting*: a child's stderr does not close because somebody pressed Apply.
    reconnect: tokio::sync::watch::Sender<u64>,
    /// The bridge inside this bundle, for the line a client is given. See `Layout::bridge`.
    bridge: std::path::PathBuf,
}

impl Inner {
    fn new(bridge: std::path::PathBuf, connection: Connection) -> Self {
        Self {
            phase: Mutex::new(Phase::Idle),
            log: Mutex::new(VecDeque::new()),
            links: tokio::sync::Mutex::new(Links::default()),
            connection: Mutex::new(connection),
            reconnect: tokio::sync::watch::channel(0).0,
            bridge,
        }
    }

    fn set(&self, phase: Phase) {
        *self.phase.lock().unwrap() = phase;
    }

    fn port(&self) -> Option<u16> {
        self.phase.lock().unwrap().port()
    }

    fn mode(&self) -> Mode {
        self.connection.lock().unwrap().mode
    }

    fn record(&self, line: String) {
        let mut log = self.log.lock().unwrap();
        if log.len() == supervisor::LOG_LINES {
            log.pop_front();
        }
        log.push_back(line);
    }

    fn status(&self) -> Status {
        let phase = self.phase.lock().unwrap().clone();
        let connection = self.connection.lock().unwrap().clone();
        let log = self.log.lock().unwrap().iter().cloned().collect();
        session::status_of(&phase, &connection, log, &self.bridge)
    }

    /// Ask the session loop to start over from whatever the settings now say.
    fn ask_for_a_restart(&self) {
        self.reconnect.send_modify(|generation| *generation += 1);
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

    //: Local mode dials a daemon this app started with a key it minted. Remote mode dials a
    //: forwarded port whose far end has no key at all -- SSH is the authentication -- and
    //: must therefore send no header rather than an empty one. See `session::authorization`.
    let authorization = session::authorization(shell.inner.mode(), &shell.key);

    let inner = shell.inner.clone();
    let closing = id.clone();
    let outbound = proxy::open(port, &path, authorization, move |frame| {
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

/// What the Connection screen renders its form from.
///
/// Carries no secret, and there is none to carry: remote mode's whole authentication story
/// is SSH, and local mode's key never leaves this process. See `settings`.
#[tauri::command]
fn conn_settings(shell: State<'_, Shell>) -> Connection {
    shell.inner.connection.lock().unwrap().clone()
}

/// Validate and persist the settings, **without** applying them.
///
/// Two decisions, kept apart on purpose: a typo saved is a typo, and a typo applied is a
/// window with no gateway behind it. The page saves, looks at what it wrote, and then asks
/// for `conn_apply`.
#[tauri::command]
fn conn_save(shell: State<'_, Shell>, settings: Connection) -> Result<Connection, String> {
    settings.validate()?;
    settings::save(&shell.layout.data_dir, &settings).map_err(|err| err.to_string())?;
    *shell.inner.connection.lock().unwrap() = settings.clone();
    Ok(settings)
}

/// The URL to frame one backend's HTML panel with, for a path `admin.panel.open` answered.
///
/// The page hands over the path it was given and gets back a `panel://` URL this process
/// answers. It is a command rather than a string the page composes because the spelling is
/// the *host's* -- WebView2 serves a custom scheme from `http://<scheme>.localhost/` and
/// every other platform from `<scheme>://localhost/` -- and a page branching on `navigator`
/// would be guessing at what its own host does. See `panelframe`.
#[tauri::command]
fn gw_panel_url(_shell: State<'_, Shell>, path: String) -> Result<String, String> {
    let token = panelframe::token_of(&path).ok_or("that is not a panel url")?;
    Ok(panelframe::framable(token))
}

/// Tear down whatever is running and start again from the saved settings.
///
/// The only way to change mode while the app is open. It returns as soon as the loop has
/// been *asked*, not when the gateway is up: what comes next arrives on `gateway-state`
/// like every other transition, and the page already knows how to render that.
#[tauri::command]
fn conn_apply(shell: State<'_, Shell>) -> Status {
    shell.inner.ask_for_a_restart();
    shell.inner.status()
}

// --- the session loop ------------------------------------------------------------------------
//
// One loop, two things it can be supervising: the gateway as a child of this process, or an
// `ssh` holding a port forward to a gateway somebody else is running. They are more alike
// than they look. Both end in a port on `127.0.0.1` that speaks the gateway's protocol, both
// die in ways that want a backoff, and both have to be interruptible by somebody pressing
// Apply in the Connection screen. What differs is spelled out in the two functions below and
// nowhere else.

/// Why an inner loop returned.
enum Outcome {
    /// The settings changed. Start again from the top, at once.
    Switch,
    /// It will not work and retrying will not help. Park until somebody asks again.
    GaveUp,
}

/// Keep a gateway reachable for as long as the app is open.
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
    // knows better. `ssh` gets it too -- for `ssh-askpass` and anything a `ProxyCommand`
    // needs -- along with the rest of the inherited environment.
    let path = pathenv::resolve();

    let mut rx = inner.reconnect.subscribe();

    loop {
        // Read at the top of every start rather than held: this is the line that makes
        // `conn_apply` mean something.
        let connection = inner.connection.lock().unwrap().clone();

        let outcome = match connection.mode {
            Mode::Local => {
                run_local(
                    &app,
                    &inner,
                    &layout,
                    &secret,
                    &path,
                    connection.local_port,
                    &mut rx,
                )
                .await
            }
            Mode::Remote => run_remote(&app, &inner, &layout, &connection, &path, &mut rx).await,
        };

        match outcome {
            // The settings changed under us and the signal has already been consumed.
            Outcome::Switch => continue,
            // Parked. The window is showing why, and the only thing that can help is a
            // person changing something -- which arrives here as a reconnect.
            Outcome::GaveUp => {
                if rx.changed().await.is_err() {
                    return;
                }
            }
        }
    }
}

/// The gateway as a child of this process: spawn it, watch it, restart it.
///
/// This is the loop this app has always had. What is new is that every long await is raced
/// against the reconnect signal, because a child's stderr does not close because somebody
/// switched to remote mode.
async fn run_local<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    inner: &Arc<Inner>,
    layout: &Layout,
    secret: &str,
    path: &str,
    port: u16,
    rx: &mut tokio::sync::watch::Receiver<u64>,
) -> Outcome {
    let announce = |phase: Phase| {
        inner.set(phase);
        let _ = app.emit(STATE_EVENT, inner.status());
    };

    let mut attempt: u32 = 0;
    let mut failed_starts: u32 = 0;

    loop {
        announce(Phase::StartingDaemon);

        // Any port file here is from a run that did not get to clean up after itself. Its
        // number is worse than useless -- something else may hold that port now -- so it
        // goes before the child that will write the real one.
        let _ = std::fs::remove_file(layout.portfile());

        let mut child = match supervisor::spawn(layout, secret, path, port) {
            Ok(child) => child,
            Err(err) => {
                announce(Phase::Failed {
                    reason: format!("could not start the gateway: {err}"),
                });
                return Outcome::GaveUp;
            }
        };

        if let Some(pid) = child.id() {
            let _ = appdata::write_pidfile(&layout.pidfile(), pid);
        }

        let started = Instant::now();

        // The port arrives by file, not by log line. Watched in its own task so it runs
        // *alongside* the stderr pump: the pump only returns when the child goes away, and
        // waiting for it first would mean nothing could connect until the daemon had died.
        let watcher = {
            let announcer = inner.clone();
            let app = app.clone();
            let path = layout.portfile();
            tauri::async_runtime::spawn(async move {
                match supervisor::wait_for_port(&path, supervisor::PORT_WAIT_TIMEOUT).await {
                    Some(port) => {
                        announcer.set(Phase::Listening { port });
                        let _ = app.emit(STATE_EVENT, announcer.status());
                        true
                    }
                    None => false,
                }
            })
        };

        // Runs until the child's stderr closes, which is how this loop learns the child is
        // going away -- or until somebody presses Apply, which is the other way out.
        let switched = {
            let recorder = inner.clone();
            let pump = async {
                if let Some(stderr) = child.stderr.take() {
                    supervisor::pump_stderr(stderr, |line| recorder.record(line)).await;
                }
            };
            tokio::select! {
                _ = pump => false,
                _ = rx.changed() => true,
            }
        };

        // stderr is closed, so the child is gone and the watcher will not find a port it has
        // not found already. Aborting rather than awaiting the full timeout is what keeps a
        // child that dies at once from delaying its own restart by a minute.
        watcher.abort();
        let reached_listening = matches!(watcher.await, Ok(true));

        stop_child(inner, layout, &mut child, &layout.pidfile()).await;
        // The daemon removes this itself on a clean exit. Doing it again covers the one it
        // cannot -- a `SIGKILL` -- and a stale port would aim the next run's sockets at
        // whatever has taken that port since.
        let _ = std::fs::remove_file(layout.portfile());

        if switched {
            announce(Phase::Idle);
            return Outcome::Switch;
        }

        if supervisor::resets_backoff(reached_listening, started.elapsed()) {
            attempt = 0;
            failed_starts = 0;
        } else if !reached_listening {
            failed_starts += 1;
        }

        if failed_starts >= supervisor::MAX_FAILED_STARTS {
            announce(Phase::Failed {
                reason: last_word(inner)
                    .unwrap_or_else(|| "the gateway exited before it could serve".into()),
            });
            return Outcome::GaveUp;
        }

        attempt += 1;
        announce(Phase::Restarting { attempt, why: None });
        if wait_or_switch(supervisor::backoff(attempt), rx).await {
            announce(Phase::Idle);
            return Outcome::Switch;
        }
    }
}

/// A gateway on another machine, reached through an `ssh` forward this app opens.
///
/// The shape is `run_local`'s and the differences are the interesting part:
///
/// * **Nothing over there is ours to start.** A far side that is not listening is not a
///   failure to retry -- it is a sentence for the window and a tunnel to keep open.
/// * **Readiness is a probe, not a file.** `ssh -L` accepts locally the moment it has
///   authenticated, so "the port accepts connections" says nothing about whether a gateway
///   is behind it. See `tunnel::probe`.
/// * **Permanent failures stop at once.** A wrong host key retried five times is five
///   identical failures and a worse message; a closed laptop lid is not a broken config and
///   retries for as long as the window is open. See `tunnel::Fault::permanent`.
async fn run_remote<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    inner: &Arc<Inner>,
    layout: &Layout,
    connection: &Connection,
    path: &str,
    rx: &mut tokio::sync::watch::Receiver<u64>,
) -> Outcome {
    let announce = |phase: Phase| {
        inner.set(phase);
        let _ = app.emit(STATE_EVENT, inner.status());
    };

    let program = tunnel::program();
    let mut attempt: u32 = 0;

    loop {
        announce(Phase::OpeningTunnel);

        // Asked before spawning, purely so the answer can be a sentence. `ssh`'s own
        // `ExitOnForwardFailure` is the authority, and it reports through one line of a log
        // somebody would have to go and read.
        if let Err(why) = tunnel::preflight(connection.local_port) {
            announce(Phase::Failed { reason: why });
            return Outcome::GaveUp;
        }

        let mut child = match tunnel::spawn(
            &program,
            &connection.destination,
            connection.local_port,
            connection.remote_port,
            path,
        ) {
            Ok(child) => child,
            Err(err) => {
                announce(Phase::Failed {
                    reason: format!("could not run {}: {err}", program.display()),
                });
                return Outcome::GaveUp;
            }
        };

        if let Some(pid) = child.id() {
            let _ = appdata::write_pidfile(&layout.tunnel_pidfile(), pid);
        }

        let started = Instant::now();
        let fault: Arc<Mutex<Option<tunnel::Fault>>> = Arc::new(Mutex::new(None));

        // Asks the far side what is behind the forward, for as long as the tunnel lives.
        // It keeps running after `Listening` because the answer can change under us: the
        // daemon over there can be stopped and started by somebody else, and this is the
        // only thing that would notice.
        let prober = {
            let inner = inner.clone();
            let app = app.clone();
            let port = connection.local_port;
            tauri::async_runtime::spawn(async move {
                let mut reached = false;
                loop {
                    let next = match tunnel::probe(port, tunnel::PROBE_TIMEOUT).await {
                        tunnel::Probe::Alive => {
                            reached = true;
                            Phase::Listening { port }
                        }
                        tunnel::Probe::FarSideRefused => Phase::FarSideSilent { port },
                        tunnel::Probe::NoListener => Phase::TunnelUp { port },
                    };
                    // `TunnelUp` before anything has answered means "ssh has not bound yet",
                    // which is the ordinary first half-second; once it has, a refused connect
                    // is the far side and not the near one.
                    let next = match next {
                        Phase::TunnelUp { port } if reached => Phase::FarSideSilent { port },
                        other => other,
                    };
                    if *inner.phase.lock().unwrap() != next {
                        inner.set(next);
                        let _ = app.emit(STATE_EVENT, inner.status());
                    }
                    tokio::time::sleep(tunnel::PROBE_INTERVAL).await;
                }
            })
        };

        let switched = {
            let recorder = inner.clone();
            let seen = fault.clone();
            let pump = async {
                if let Some(stderr) = child.stderr.take() {
                    supervisor::pump_stderr(stderr, |line| {
                        if let Some(kind) = tunnel::classify(&line) {
                            *seen.lock().unwrap() = Some(kind);
                        }
                        recorder.record(line);
                    })
                    .await;
                }
            };
            tokio::select! {
                _ = pump => false,
                _ = rx.changed() => true,
            }
        };

        prober.abort();
        let _ = prober.await;
        stop_child(inner, layout, &mut child, &layout.tunnel_pidfile()).await;

        if switched {
            announce(Phase::Idle);
            return Outcome::Switch;
        }

        let fault = *fault.lock().unwrap();

        // A tunnel that came up and stayed up for a while and then dropped is a fresh
        // problem, not a continuation of the last one.
        if supervisor::resets_backoff(true, started.elapsed()) {
            attempt = 0;
        }

        if let Some(kind) = fault.filter(|kind| kind.permanent()) {
            announce(Phase::Failed {
                reason: format!(
                    "{} {}",
                    last_word(inner).unwrap_or_else(|| "The tunnel could not be opened.".into()),
                    kind.hint()
                ),
            });
            return Outcome::GaveUp;
        }

        attempt += 1;
        announce(Phase::Restarting {
            attempt,
            why: Some(match fault {
                Some(kind) => format!(
                    "{} {}",
                    last_word(inner).unwrap_or_else(|| "The tunnel dropped.".into()),
                    kind.hint()
                ),
                None => format!(
                    "The tunnel to {} dropped; reopening (attempt {attempt})…",
                    connection.destination
                ),
            }),
        });
        if wait_or_switch(supervisor::backoff(attempt), rx).await {
            announce(Phase::Idle);
            return Outcome::Switch;
        }
    }
}

/// Reap a child and drop every socket that was pointed through it.
///
/// Shared by both loops, because it is the same job either way: the daemon is gone, or the
/// forward is, and in both cases every live socket now points at a port that no longer
/// answers. Dropping the registry's senders ends each pump, which sends the window a close
/// frame -- and that is what makes `rpc.js` start its own reconnect rather than waiting on a
/// socket that will never speak again.
async fn stop_child(
    inner: &Arc<Inner>,
    _layout: &Layout,
    child: &mut tokio::process::Child,
    pidfile: &std::path::Path,
) {
    supervisor::terminate(child).await;
    appdata::clear_pidfile(pidfile);
    inner.links.lock().await.drain();
}

/// The last thing the child said that was worth repeating.
fn last_word(inner: &Arc<Inner>) -> Option<String> {
    inner
        .log
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|line| !line.trim().is_empty())
        .cloned()
}

/// Sleep, unless somebody presses Apply first. `true` means they did.
async fn wait_or_switch(
    how_long: std::time::Duration,
    rx: &mut tokio::sync::watch::Receiver<u64>,
) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(how_long) => false,
        _ = rx.changed() => true,
    }
}

/// Answer one `panel://` request: the daemon's bytes under the daemon's policy, or a refusal.
///
/// **Every failure is a document rather than a status alone**, because the only thing that
/// will ever read this is an `<iframe>` in the results column, and an empty frame is the one
/// outcome that tells a person nothing. The refusals are deliberately plain text: they are
/// this process speaking, not a backend, and nothing here should be able to put markup on
/// the screen that did not come through the policy below.
async fn serve_panel(port: Option<u16>, path: &str) -> tauri::http::Response<Vec<u8>> {
    let Some(port) = port else {
        return refusal("The gateway is not running, so there is no panel to show.");
    };
    let Some(token) = panelframe::token_of(path) else {
        return refusal("That is not a panel address.");
    };
    let panel = match panelframe::fetch(port, token).await {
        Ok(panel) => panel,
        Err(err) => return refusal(&format!("That panel could not be read: {err}")),
    };
    if panel.status != 200 {
        // A spent or lapsed token, which is ordinary: a panel URL is single-use and lives
        // for seconds. Saying so beats a blank frame.
        return refusal("That panel URL has already been used, or has expired.");
    }
    if panel.csp.is_empty() {
        // The daemon always sends one. If it did not, this process is not the place to
        // invent one -- serving a backend's document with no policy at all is the failure
        // this whole module exists to prevent.
        return refusal("That panel arrived without a content security policy; refusing it.");
    }
    tauri::http::Response::builder()
        .status(200)
        .header(
            "Content-Type",
            if panel.content_type.is_empty() {
                "text/html; charset=utf-8"
            } else {
                &panel.content_type
            },
        )
        // Verbatim, never composed here. See `panelframe`.
        .header("Content-Security-Policy", &panel.csp)
        .header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .body(panel.body)
        .unwrap_or_else(|_| refusal("That panel could not be delivered."))
}

/// What the frame shows when there is nothing to show. Plain text under a policy that
/// permits nothing at all, because this is the shell talking.
fn refusal(why: &str) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .status(404)
        .header("Content-Type", "text/plain; charset=utf-8")
        .header("Content-Security-Policy", "sandbox; default-src 'none'")
        .body(format!("{why}\n").into_bytes())
        .expect("a static response builds")
}

/// Route a `SIGTERM` or a `Ctrl-C` into the app's own exit, so the teardown runs.
///
/// Without this the quit path depends on *how* the app was asked to stop: a menu quit runs
/// `RunEvent::Exit` and a signal does not, so the same app stops its gateway or strands it
/// depending on whether somebody used the menu. Both were seen -- a release bundle stopped
/// with `SIGTERM` left its daemon running, and a `cargo tauri dev` interrupted at the
/// terminal is how most of the orphans in python-mcp-gateway-g90.10 were made.
///
/// `SIGKILL` and Force Quit remain unreachable from here by definition. That is what the
/// pidfile layer is for, and why `stop_recorded` keeps the record when it cannot confirm a
/// death.
#[cfg(unix)]
fn watch_for_a_signal(handle: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        use tokio::signal::unix::{signal, SignalKind};
        let (Ok(mut term), Ok(mut interrupt)) = (
            signal(SignalKind::terminate()),
            signal(SignalKind::interrupt()),
        ) else {
            // A handler this process cannot install is not a reason to refuse to start; it
            // is one more way to end up in the Force Quit case, which the pidfile covers.
            return;
        };
        tokio::select! {
            _ = term.recv() => {},
            _ = interrupt.recv() => {},
        }
        // Not `stop_children` directly: going through the app's own exit means one teardown
        // with one ordering, rather than a second path that has to be kept in step.
        handle.exit(0);
    });
}

#[cfg(not(unix))]
fn watch_for_a_signal(_handle: tauri::AppHandle) {}

/// Stop both children on the way out, and keep the record of any that would not go.
///
/// **This used to clear the two pidfiles and nothing else**, on the premise that
/// `kill_on_drop` had the rest. It does not: the app exits without unwinding -- `panic =
/// "abort"` is in `Cargo.toml` and a process exit drops no detached task -- so nothing was
/// ever signalled, and deleting the pidfiles threw away the one record that would have let
/// the next launch find what had been left behind. The cost was seventeen orphaned daemons
/// on one developer's machine, the oldest eight days old, each still holding the backend
/// subprocesses it had spawned (python-mcp-gateway-g90.10).
///
/// So: signal the group, wait, `SIGKILL` if it is still there, and clear each pidfile only
/// once its process is gone. A child that survives all of that keeps its record, which is
/// what `reap_previous` reads on the next launch. See `appdata::stop_recorded`.
pub(crate) fn stop_children(layout: &Layout) {
    // Two children, two records. See `Layout::tunnel_pidfile`.
    appdata::stop_recorded(&layout.pidfile(), &layout.interpreter());
    appdata::stop_recorded(&layout.tunnel_pidfile(), &tunnel::program());
}

// --- the app -----------------------------------------------------------------------------

/// Start the app.
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            gw_open,
            gw_send,
            gw_close,
            gw_status,
            conn_settings,
            conn_save,
            conn_apply,
            gw_panel_url
        ])
        // The one document this window loads that nobody here wrote. The handler fetches it
        // from the daemon and copies the daemon's own `Content-Security-Policy` onto the
        // answer, `sandbox allow-scripts` included -- so the panel lands in an opaque origin
        // and cannot reach this window, its storage, or the IPC. See `panelframe`.
        .register_asynchronous_uri_scheme_protocol(panelframe::SCHEME, |app, request, responder| {
            let port = app.app_handle().state::<Shell>().inner.port();
            let path = request.uri().path().to_string();
            tauri::async_runtime::spawn(async move {
                responder.respond(serve_panel(port, &path).await);
            });
        })
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
            // And the same for a forward: an `ssh` nobody killed still holds the local port,
            // which would make this launch's preflight blame the user for the last launch's
            // corpse.
            appdata::reap_previous(&layout.tunnel_pidfile(), &tunnel::program());

            // A file that is there and unusable is not the same as no file at all, so a
            // complaint goes into the log ring the gate already shows rather than being
            // swallowed on the way to the local default.
            let (connection, complaint) = settings::load(&data_dir);
            let inner = Arc::new(Inner::new(layout.bridge(), connection));
            if let Some(complaint) = complaint {
                inner.record(complaint);
            }

            app.manage(Shell {
                key: AccessKey::mint(),
                layout,
                inner: inner.clone(),
            });

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(supervise(handle, inner));
            watch_for_a_signal(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the app could not be built")
        .run(|app, event| {
            // Quitting must not leave a gateway -- or its backends -- running. This is the
            // first of the three layers described in `supervisor`; the other two are
            // `kill_on_drop` and the pidfile.
            if let RunEvent::Exit = event {
                stop_children(&app.state::<Shell>().layout);
            }
        });
}
