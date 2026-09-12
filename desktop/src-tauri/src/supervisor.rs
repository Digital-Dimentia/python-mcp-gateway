//! The gateway process: starting it, learning its port, keeping it up, and killing it dead.
//!
//! ## Learning the port
//!
//! The child is spawned with `--port 0`, so the OS picks. That is not a detail: a desktop
//! app that hard-coded 8765 would collide with the `make run` daemon a developer already
//! has open, and the second one to bind would simply fail.
//!
//! The port comes back out of the daemon's own startup line on stderr:
//!
//! ```text
//! listening on ws://127.0.0.1:49613/mcp
//! ```
//!
//! which is emitted by `transport_ws.py` *after* the backend pool is up, because
//! `Gateway.start()` spawns backends before it binds. So one line is both the port and the
//! readiness signal, and no new Python surface was invented for this.
//!
//! Binding an ephemeral port here and passing `--port N` was considered and rejected: it is
//! a time-of-check-to-time-of-use race for nothing, when the daemon can just say.
//!
//! The cost is that a log line is now a wire format. `tests/test_desktop_contract.py` pins
//! it from the Python side, so a change there fails a Python test rather than a dev build
//! nobody runs in CI. Replacing this with a real handshake (`--port-file`, or an inherited
//! socket) is filed rather than guessed at -- it is Python surface added for one consumer.
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
const LISTENING_MARKER: &str = "listening on ws://";

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

/// The port out of the daemon's startup line, or `None` if this is not that line.
///
/// **Twinned with `parse_listening` in `tests/test_desktop_contract.py`.** If one learns
/// something, teach the other.
///
/// A substring search rather than a line anchor, because the default log format is a bare
/// `%(message)s` but `--debug` prefixes the logger name -- and a shell that only worked
/// without `--debug` would break exactly when someone turned logging up to find out why.
/// `rsplit` on the colon, so an IPv6 authority like `[::1]:8765` still yields its port.
pub fn parse_listening(line: &str) -> Option<u16> {
    let rest = line.split_once(LISTENING_MARKER)?.1;
    let authority = rest.split('/').next()?;
    let (_, port) = authority.rsplit_once(':')?;
    port.trim().parse().ok()
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
}

/// The argv the gateway is started with.
///
/// A function so it is testable, and so the one place that decides `--port 0` is visible.
/// `--env` is deliberately absent: `cli.default_env_path` resolves `gateway.env` beside the
/// resolved config, which is where the seeding put it. One fewer path to keep in sync.
pub fn argv(config: &Path) -> Vec<String> {
    vec![
        "-m".into(),
        "mcp_gateway.cli".into(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        "0".into(),
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
        .args(argv(&layout.config()))
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
/// sequential, which meant nothing could react to the port until the child had already
/// gone -- fine for the supervisor loop, useless for anything that wants to connect.
pub async fn pump_stderr<L, P>(stderr: ChildStderr, mut on_line: L, mut on_port: P)
where
    L: FnMut(String),
    P: FnMut(u16),
{
    let mut lines = BufReader::new(stderr).lines();
    let mut announced = false;
    while let Ok(Some(line)) = lines.next_line().await {
        if !announced {
            if let Some(port) = parse_listening(&line) {
                announced = true;
                on_port(port);
            }
        }
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

    // --- the startup line -------------------------------------------------------------

    #[test]
    fn the_port_comes_out_of_the_daemons_own_line() {
        assert_eq!(
            parse_listening("listening on ws://127.0.0.1:49613/mcp"),
            Some(49613)
        );
    }

    #[test]
    fn debug_logging_prefixes_the_line_and_must_not_break_it() {
        assert_eq!(
            parse_listening("mcp_gateway.transport_ws: listening on ws://127.0.0.1:8765/mcp"),
            Some(8765)
        );
    }

    #[test]
    fn an_ipv6_authority_still_yields_its_port() {
        assert_eq!(
            parse_listening("listening on ws://[::1]:8765/mcp"),
            Some(8765)
        );
    }

    #[test]
    fn the_other_startup_lines_are_not_mistaken_for_it() {
        // Both of these are real lines the daemon prints, next to the one we want.
        assert_eq!(parse_listening("server listening on 127.0.0.1:49613"), None);
        assert_eq!(
            parse_listening("admin UI at http://127.0.0.1:49613/ui/"),
            None
        );
    }

    #[test]
    fn a_malformed_line_is_none_rather_than_a_wrong_port() {
        assert_eq!(parse_listening(""), None);
        assert_eq!(parse_listening("listening on ws://127.0.0.1/mcp"), None);
        assert_eq!(
            parse_listening("listening on ws://127.0.0.1:notaport/mcp"),
            None
        );
        assert_eq!(parse_listening("listening on ws://"), None);
        // Truncated mid-write, which a line-buffered reader can genuinely hand us.
        assert_eq!(
            parse_listening("listening on ws://127.0.0.1:4961"),
            Some(4961)
        );
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
        let args = argv(Path::new("/data/servers.yaml"));
        let port = args.iter().position(|a| a == "--port").expect("--port");
        assert_eq!(
            args[port + 1],
            "0",
            "a fixed port would collide with `make run`"
        );
    }

    #[test]
    fn the_child_is_told_where_its_config_is_and_not_where_its_secrets_are() {
        let args = argv(Path::new("/data/servers.yaml"));
        assert!(args.contains(&"/data/servers.yaml".to_string()));
        // `cli.default_env_path` finds `gateway.env` beside the config. Passing `--env`
        // would be a second path to keep in sync with the seeding.
        assert!(!args.iter().any(|a| a == "--env"));
    }

    #[test]
    fn the_key_travels_in_the_environment_and_never_in_argv() {
        let key = "a-secret-that-must-not-be-public";
        let args = argv(Path::new("/data/servers.yaml"));
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
