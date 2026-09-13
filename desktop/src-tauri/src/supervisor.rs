//! The gateway process: starting it, learning its port, keeping it up, and killing it dead.
//!
//! ## Learning the port
//!
//! The child is spawned with `--port 0`, so the OS picks. That is not a detail: a desktop
//! app that hard-coded 8765 would collide with the `make run` daemon a developer already
//! has open, and the second one to bind would simply fail.
//!
//! The child is given `--port-file <path>` and writes the bound port there once the socket
//! exists, atomically, removing it on the way out. `wait_for_port` polls for it. The file's
//! *appearance* is the readiness signal, so one mechanism carries both facts and there is
//! nothing to parse beyond a decimal number.
//!
//! Binding an ephemeral port here and passing `--port N` was considered and rejected: it is
//! a time-of-check-to-time-of-use race for nothing, when the daemon can just say.
//!
//! This used to be a scrape of the daemon's own startup line -- `listening on
//! ws://127.0.0.1:49613/mcp` -- which worked and was pinned from both sides, but made a
//! sentence written for a human into a wire format nobody could reword. `--port-file` is
//! what daemons have always offered for this, it generalises past this one caller to
//! anything starting the gateway on `--port 0`, and it removes the twinned parser that had
//! to be kept in step across two languages. See python-mcp-gateway-9p3.
//!
//! stderr is still read, because the log pane and the failure reason need it. What it is no
//! longer responsible for is the port.
//!
//! ## Not leaving processes behind
//!
//! The gateway owns subprocesses of its own, and they hold every credential in
//! `gateway.env`. A shell that exits and leaves them running is not untidy, it is a
//! security bug that accumulates. macOS has no `PDEATHSIG`, so there are three layers:
//!
//! 1. The child is put in its own process group (`setpgid`), and teardown signals the
//!    *group*. That takes the `npx` backends with it even if the daemon itself is wedged.
//! 2. `kill_on_drop`, for the panic path.
//! 3. A pidfile, checked at startup, for the case neither of the above can reach: the shell
//!    itself being `SIGKILL`ed (Force Quit).

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, ChildStderr, Command};

/// The marker the port is read out of. See the module docs.
/// How often `wait_for_port` looks for the file, and how long it looks for.
///
/// The timeout is generous because the thing it is waiting on is a cold Python interpreter
/// importing the world, on a machine that may be doing a great deal else at login.
const PORT_POLL_INTERVAL: Duration = Duration::from_millis(50);
pub const PORT_WAIT_TIMEOUT: Duration = Duration::from_secs(60);

/// How long a spawn gets to reach [`State::Listening`] before it counts as a failed start.
///
/// Generous, because this clock covers the backend pool coming up: a `servers.yaml` full of
/// `npx` entries on a cold npm cache is genuinely slow the first time.
pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);

/// The same schedule `rpc.js` uses for its own reconnect, so the window and the process
/// behind it recover on one rhythm rather than two.
pub const BACKOFF_MS: &[u64] = &[250, 500, 1000, 2000, 4000, 8000];

/// How many starts that never reached [`State::Listening`] before giving up.
///
/// A config the daemon refuses is not a transient, and retrying it forever produces a
/// window that says "connecting" for the rest of the afternoon. The reason it refused is in
/// the log buffer and belongs in front of the user instead.
pub const MAX_FAILED_STARTS: u32 = 5;

/// How long a run has to last before it counts as healthy enough to reset the backoff.
const HEALTHY_AFTER: Duration = Duration::from_secs(10);

/// How long `SIGTERM` gets before `SIGKILL`.
const TERM_GRACE: Duration = Duration::from_secs(5);

/// Lines of the child's stderr kept for the UI and for a bug report.
pub const LOG_LINES: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum State {
    Idle,
    Starting,
    Listening {
        port: u16,
    },
    Restarting {
        attempt: u32,
    },
    /// Gave up. `reason` is what the daemon last said, which is the part worth showing.
    Failed {
        reason: String,
    },
}

/// The port in a port file, or `None` if it is absent, empty or not a port.
///
/// **Twinned with `portfile.read` in `src/mcp_gateway/portfile.py`.** If one learns
/// something, teach the other -- though there is far less to learn than the log line it
/// replaced: one decimal number, and every other state means "keep waiting".
///
/// Tolerant rather than strict, because a reader polling for this file is racing a writer.
/// An empty read is a state to wait through, not an error to report.
pub fn parse_port_file(contents: &str) -> Option<u16> {
    let text = contents.trim();
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match text.parse::<u16>() {
        Ok(port) if port >= 1 => Some(port),
        _ => None,
    }
}

/// Wait for the child to publish its port, giving up after `timeout`.
///
/// Polled rather than watched. A filesystem notification API would save these wakeups, but
/// it is a dependency and a per-platform behaviour difference to buy a saving measured
/// against a file that appears within a second or two of a process starting -- and the
/// poll has to exist anyway as the fallback for the platforms where the watch is unreliable.
///
/// The file is removed by the daemon on exit, so a stale one from a killed process is
/// possible. That is why the caller deletes any existing file *before* spawning: an old
/// number here would send the shell's sockets at whatever now owns that port.
pub async fn wait_for_port(path: &Path, timeout: Duration) -> Option<u16> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if let Ok(contents) = tokio::fs::read_to_string(path).await {
            if let Some(port) = parse_port_file(&contents) {
                return Some(port);
            }
        }
        if tokio::time::Instant::now() >= deadline {
            return None;
        }
        tokio::time::sleep(PORT_POLL_INTERVAL).await;
    }
}

/// The delay before attempt `attempt` (1-based), capped at the last step.
pub fn backoff(attempt: u32) -> Duration {
    let index = (attempt.max(1) - 1) as usize;
    Duration::from_millis(BACKOFF_MS[index.min(BACKOFF_MS.len() - 1)])
}

/// Whether a run that lasted `uptime` earns a reset of the backoff schedule.
pub fn resets_backoff(reached_listening: bool, uptime: Duration) -> bool {
    reached_listening && uptime >= HEALTHY_AFTER
}

/// Where everything the child needs lives.
#[derive(Debug, Clone)]
pub struct Layout {
    /// The bundled interpreter's root: `<resources>/python`.
    pub python_root: PathBuf,
    /// The app data directory holding `servers.yaml` and `gateway.env`.
    pub data_dir: PathBuf,
}

impl Layout {
    pub fn interpreter(&self) -> PathBuf {
        if cfg!(windows) {
            self.python_root.join("python.exe")
        } else {
            self.python_root.join("bin").join("python3")
        }
    }

    pub fn config(&self) -> PathBuf {
        self.data_dir.join("servers.yaml")
    }

    pub fn pidfile(&self) -> PathBuf {
        self.data_dir.join("gateway.pid")
    }

    pub fn portfile(&self) -> PathBuf {
        self.data_dir.join("gateway.port")
    }
}

/// The argv the gateway is started with.
///
/// A function so it is testable, and so the one place that decides `--port 0` is visible.
/// `--env` is deliberately absent: `cli.default_env_path` resolves `gateway.env` beside the
/// resolved config, which is where the seeding put it. One fewer path to keep in sync.
pub fn argv(config: &Path, port_file: &Path) -> Vec<String> {
    vec![
        "-m".into(),
        "mcp_gateway.cli".into(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        "0".into(),
        "--port-file".into(),
        port_file.to_string_lossy().into_owned(),
        "--config".into(),
        config.to_string_lossy().into_owned(),
    ]
}

/// Build the child's environment.
///
/// Deliberately additive rather than `env_clear()`: `backend.py` supports an
/// `env_passthrough` list that reads the *daemon's* environment, so a cleared environment
/// would silently change what that feature can see. The app is not the place to redefine a
/// documented behaviour of the daemon. What is set here is set on purpose:
///
/// * `PATH` -- repaired, see `pathenv`. This is the one that makes `npx` backends work.
/// * `MCP_GATEWAY_WS_KEY` -- the minted key, in the environment because argv is public.
/// * `PYTHONUNBUFFERED` -- the port is read off stderr; a buffered one would arrive late.
/// * `PYTHONDONTWRITEBYTECODE` -- the interpreter lives inside the `.app`, which is
///   read-only once signed, and doubly so under Gatekeeper path translocation.
///   `bundle_python.py` pre-compiled everything, so nothing is lost.
pub fn environment(key: &str, path: &str) -> Vec<(String, String)> {
    vec![
        ("PATH".into(), path.into()),
        (crate::key::ACCESS_KEY_ENV.into(), key.into()),
        ("PYTHONUNBUFFERED".into(), "1".into()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
    ]
}

/// Spawn the gateway. stdout is discarded; stderr is the channel everything is read from.
pub fn spawn(layout: &Layout, key: &str, path: &str) -> std::io::Result<Child> {
    let mut command = Command::new(layout.interpreter());
    command
        .args(argv(&layout.config(), &layout.portfile()))
        .envs(environment(key, path))
        // The gateway does not read stdin, and a pipe it never reads is a file descriptor
        // to leak. Null, so anything that did read gets EOF rather than blocking.
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    #[cfg(unix)]
    unsafe {
        // Its own process group, so teardown can signal the whole tree -- the daemon and
        // every backend it spawned. See the module docs.
        command.pre_exec(|| {
            if libc::setpgid(0, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }

    command.spawn()
}

/// Read the child's stderr, handing every line to `on_line` and the port to `on_port`.
///
/// Returns when stderr closes, which is when the child has exited or closed the descriptor.
///
/// Takes the handle rather than borrowing the `Child`, so it can be spawned as a task and
/// run *alongside* waiting on the process. The borrowing form forced the two to be
/// sequential, which meant nothing could react until the child had already gone -- fine for
/// the supervisor loop, useless for anything that wants to watch the log as it happens.
///
/// Log lines only. The port arrives by `wait_for_port` now; see the module docs.
pub async fn pump_stderr<L>(stderr: ChildStderr, mut on_line: L)
where
    L: FnMut(String),
{
    let mut lines = BufReader::new(stderr).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        on_line(line);
    }
}

/// Signal the child's whole process group, then the child, then give up on being polite.
///
/// The group first: the daemon's own `SIGTERM` handler stops it cleanly, but if it is
/// wedged mid-`tools/call` its `npx` children would outlive it, and those are the processes
/// holding credentials.
#[cfg(unix)]
pub async fn terminate(child: &mut Child) {
    let Some(pid) = child.id() else { return };
    let pid = pid as libc::pid_t;

    unsafe {
        // Negative pid means "the group". `setpgid` above made the child its own leader, so
        // this reaches the daemon and everything under it and nothing else.
        libc::kill(-pid, libc::SIGTERM);
    }

    if tokio::time::timeout(TERM_GRACE, child.wait()).await.is_ok() {
        return;
    }

    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    let _ = child.wait().await;
}

#[cfg(not(unix))]
pub async fn terminate(child: &mut Child) {
    let _ = child.kill().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- the port file ------------------------------------------------------------------

    #[test]
    fn the_port_comes_out_of_the_file_the_daemon_wrote() {
        assert_eq!(parse_port_file("49613\n"), Some(49613));
        assert_eq!(parse_port_file("8765"), Some(8765));
        assert_eq!(parse_port_file("  8765  \n"), Some(8765));
    }

    #[test]
    fn a_half_written_file_is_none_rather_than_a_wrong_port() {
        // The writer renames into place so this should not happen, but a reader that is
        // polling has no way to know that and must not turn a partial read into a port.
        assert_eq!(parse_port_file(""), None);
        assert_eq!(parse_port_file("   "), None);
        assert_eq!(parse_port_file("notaport"), None);
        assert_eq!(parse_port_file("8765x"), None);
        assert_eq!(parse_port_file("-1"), None);
    }

    #[test]
    fn a_number_that_is_not_a_port_is_refused() {
        assert_eq!(parse_port_file("0"), None);
        assert_eq!(parse_port_file("65536"), None);
        assert_eq!(parse_port_file("99999999"), None);
    }

    #[tokio::test]
    async fn waiting_returns_the_port_once_the_file_appears() {
        let dir = std::env::temp_dir().join(format!("mcpgw-port-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("gateway.port");
        let _ = std::fs::remove_file(&path);

        let writing = path.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(120)).await;
            std::fs::write(&writing, "51234\n").unwrap();
        });

        let port = wait_for_port(&path, Duration::from_secs(5)).await;
        assert_eq!(port, Some(51234));
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn waiting_gives_up_rather_than_hanging_when_nothing_appears() {
        let path = std::env::temp_dir().join("mcpgw-port-that-never-exists");
        let _ = std::fs::remove_file(&path);
        assert_eq!(wait_for_port(&path, Duration::from_millis(150)).await, None);
    }

    // --- the restart schedule ---------------------------------------------------------

    #[test]
    fn the_backoff_is_the_one_the_ui_uses() {
        let schedule: Vec<u64> = (1..=BACKOFF_MS.len() as u32)
            .map(|n| backoff(n).as_millis() as u64)
            .collect();
        assert_eq!(schedule, BACKOFF_MS);
    }

    #[test]
    fn the_backoff_caps_rather_than_doubling_forever() {
        let last = *BACKOFF_MS.last().unwrap();
        assert_eq!(backoff(99).as_millis() as u64, last);
        // Attempt 0 is not a thing, but it must not panic if it ever happens.
        assert_eq!(backoff(0).as_millis() as u64, BACKOFF_MS[0]);
    }

    #[test]
    fn only_a_run_that_actually_served_resets_the_schedule() {
        assert!(resets_backoff(true, Duration::from_secs(30)));
        // Up, but only briefly: a crash loop that binds first is still a crash loop.
        assert!(!resets_backoff(true, Duration::from_secs(1)));
        // Long-lived, but never bound: a config it refuses does not get easier by waiting.
        assert!(!resets_backoff(false, Duration::from_secs(30)));
    }

    // --- how the child is started ------------------------------------------------------

    #[test]
    fn the_child_is_told_to_let_the_os_pick_the_port() {
        let args = argv(Path::new("/data/servers.yaml"), Path::new("/data/gateway.port"));
        let port = args.iter().position(|a| a == "--port").expect("--port");
        assert_eq!(
            args[port + 1],
            "0",
            "a fixed port would collide with `make run`"
        );
    }

    #[test]
    fn the_child_is_told_where_its_config_is_and_not_where_its_secrets_are() {
        let args = argv(Path::new("/data/servers.yaml"), Path::new("/data/gateway.port"));
        assert!(args.contains(&"/data/servers.yaml".to_string()));
        // `cli.default_env_path` finds `gateway.env` beside the config. Passing `--env`
        // would be a second path to keep in sync with the seeding.
        assert!(!args.iter().any(|a| a == "--env"));
    }

    #[test]
    fn the_key_travels_in_the_environment_and_never_in_argv() {
        let key = "a-secret-that-must-not-be-public";
        let args = argv(Path::new("/data/servers.yaml"), Path::new("/data/gateway.port"));
        assert!(
            !args.iter().any(|a| a.contains(key)),
            "argv is world-readable through `ps`"
        );
        let env = environment(key, "/usr/bin");
        assert!(env
            .iter()
            .any(|(k, v)| k == crate::key::ACCESS_KEY_ENV && v == key));
    }

    #[test]
    fn the_child_gets_the_repaired_path_and_an_unwritable_bundle_is_accounted_for() {
        let env = environment("k", "/opt/homebrew/bin:/usr/bin");
        let get = |name: &str| env.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str());
        assert_eq!(get("PATH"), Some("/opt/homebrew/bin:/usr/bin"));
        // Unbuffered, or the port arrives after we have given up waiting for it.
        assert_eq!(get("PYTHONUNBUFFERED"), Some("1"));
        // The resources directory inside a signed .app cannot be written to.
        assert_eq!(get("PYTHONDONTWRITEBYTECODE"), Some("1"));
    }

    #[test]
    fn the_environment_is_additive_so_env_passthrough_still_works() {
        // `backend.py`'s `env_passthrough` reads the daemon's own environment. Clearing it
        // here would silently change a documented feature of the daemon, so this list is
        // what gets *added*, and is deliberately short.
        let env = environment("k", "/usr/bin");
        assert_eq!(
            env.len(),
            4,
            "every addition here changes what backends can see"
        );
    }

    // --- the layout ---------------------------------------------------------------------

    #[test]
    fn the_interpreter_is_where_bundle_python_put_it() {
        let layout = Layout {
            python_root: PathBuf::from("/App.app/Contents/Resources/python"),
            data_dir: PathBuf::from("/data"),
        };
        // `scripts/bundle_python.py` flattens uv's versioned directory to exactly this, so
        // a patch release does not move the path this side names.
        #[cfg(unix)]
        assert!(layout.interpreter().ends_with("python/bin/python3"));
        assert!(layout.config().ends_with("servers.yaml"));
        assert!(layout.pidfile().ends_with("gateway.pid"));
    }
}
