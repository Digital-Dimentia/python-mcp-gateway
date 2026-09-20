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
//! security bug that accumulates. Every platform has to answer the same two questions --
//! "kill the whole tree, not just the daemon" and "what if the shell itself is killed" --
//! and each answers them with a different primitive.
//!
//! **macOS.** No `PDEATHSIG`, so three layers:
//!
//! 1. The child is put in its own process group (`setpgid`), and teardown signals the
//!    *group*. That takes the `npx` backends with it even if the daemon itself is wedged.
//! 2. `kill_on_drop`, for the panic path.
//! 3. A pidfile, checked at startup, for the case neither of the above can reach: the shell
//!    itself being `SIGKILL`ed (Force Quit).
//!
//! ⚠️ **Layer 1 only exists if something signals the group, and for a long time nothing
//! did.** The app's exit handler cleared the two pidfiles and relied on `kill_on_drop` --
//! which cannot fire, because the process exits without unwinding and `panic = "abort"` is
//! in `Cargo.toml`. So quitting signalled nothing and then deleted the record that would
//! have let the next launch find the child. Seventeen orphaned daemons, the oldest eight
//! days, each still holding its backends (python-mcp-gateway-g90.10). `lib::stop_children`
//! is layer 1 now, `appdata::stop_recorded` is what it calls, and the rule that came out of
//! it is: **a pidfile is cleared only once its process is confirmed gone.** A record that
//! outlives a failed kill is the whole point of layer 3; a record deleted beside a live
//! child is worse than never having written one.
//!
//! **Linux.** The same three, plus `prctl(PR_SET_PDEATHSIG, SIGTERM)` in the child, which
//! is strictly better than all of them: the kernel signals the daemon the moment this
//! process goes, including the `SIGKILL` no handler of ours can run on. `SIGTERM` and not
//! `SIGKILL`, so the daemon's own handler gets to take its backends down cleanly. The
//! other three stay because `PDEATHSIG` has one hole -- it fires when the *thread* that
//! spawned the child exits, not the process -- and because a daemon that ignores `SIGTERM`
//! still needs the group kill.
//!
//! **Windows.** No process groups and no `PDEATHSIG`. The equivalent is a Job Object with
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: every gateway this process starts gets a job of
//! its own, `TerminateJobObject` on that job is the group kill, and the *close* limit is
//! the Force Quit answer -- when this process dies by any means, its handles close and the
//! kernel kills everything still in those jobs. That is the pidfile layer done by the OS,
//! which is why `appdata::reap_previous` is a no-op there.
//!
//! One job **per child**, so that `terminate(child)` names the same blast radius on both
//! platforms: that daemon and its descendants. The app spawns one gateway and could not
//! tell the difference; the integration tests spawn three from one process and could.

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

/// How long `SIGTERM` gets before `SIGKILL`. Unix only -- Windows has no polite signal to
/// wait on, so its teardown has no grace period to name. Left uncompiled rather than
/// unused there, because `tauri-check` runs `clippy -D warnings` on every platform.
#[cfg(unix)]
const TERM_GRACE: Duration = Duration::from_secs(5);

/// Lines of the child's stderr kept for the UI and for a bug report.
pub const LOG_LINES: usize = 500;

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

    /// The forward's pidfile, kept apart from the daemon's.
    ///
    /// Two children, two records. A Force Quit that leaves an `ssh` holding the forwarded
    /// port would otherwise make the next launch's preflight blame the user for this app's
    /// own corpse. See `appdata::reap_previous`.
    pub fn tunnel_pidfile(&self) -> PathBuf {
        self.data_dir.join("tunnel.pid")
    }

    /// The bridge inside this bundle: what a client on this machine is pointed at.
    ///
    /// Named here rather than assembled in the page, because the page does not know where
    /// the app was installed and a line that only works from `/Applications` is worse than
    /// no line at all. See `session::connect_command`.
    pub fn bridge(&self) -> PathBuf {
        if cfg!(windows) {
            self.python_root
                .join("Scripts")
                .join("mcp-gateway-connect.exe")
        } else {
            self.python_root.join("bin").join("mcp-gateway-connect")
        }
    }
}

/// The argv the gateway is started with.
///
/// A function so it is testable, and so the one place that decides the port is visible.
/// `--env` is deliberately absent: `cli.default_env_path` resolves `gateway.env` beside the
/// resolved config, which is where the seeding put it. One fewer path to keep in sync.
///
/// `port` is `0` unless someone has asked for a fixed one in `connection.json`, and `0` is
/// what this app passed for its whole life before that file existed: let the OS pick, and
/// read the answer back out of the port file. The reason to allow a number is that an
/// ephemeral port cannot be written into a client's config -- `mcp-gateway-connect --url
/// ws://127.0.0.1:<n>/mcp` has to name a port that is still there tomorrow. See
/// `settings::Connection::local_port`.
pub fn argv(config: &Path, port_file: &Path, port: u16) -> Vec<String> {
    vec![
        "-m".into(),
        "mcp_gateway.cli".into(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        port.to_string(),
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
pub fn spawn(layout: &Layout, key: &str, path: &str, port: u16) -> std::io::Result<Child> {
    let mut command = Command::new(layout.interpreter());
    command
        .args(argv(&layout.config(), &layout.portfile(), port))
        .envs(environment(key, path))
        // The gateway does not read stdin, and a pipe it never reads is a file descriptor
        // to leak. Null, so anything that did read gets EOF rather than blocking.
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    spawn_hardened(&mut command)
}

/// Spawn a child that cannot outlive this app, whatever happens to either of them.
///
/// The three layers in the module docs, in one place: its own process group so `terminate`
/// can signal the whole tree, `PR_SET_PDEATHSIG` on Linux for the SIGKILL no exit handler
/// survives, and a Job Object on Windows, which has neither. The caller brings argv, the
/// environment and the stdio; this brings the orphan control.
///
/// Extracted when the shell learned to supervise a second kind of child -- an `ssh` holding
/// a port forward, in `tunnel.rs`. A tunnel that outlives the window is the same bug as a
/// gateway that does, and a second copy of this code is how one of them would quietly stop
/// being true.
pub fn spawn_hardened(command: &mut Command) -> std::io::Result<Child> {
    #[cfg(unix)]
    unsafe {
        // Captured here, in the parent, because the child cannot ask what it used to be.
        // See the `PDEATHSIG` race below.
        let parent = std::process::id() as libc::pid_t;
        command.pre_exec(move || {
            // Its own process group, so teardown can signal the whole tree -- the daemon
            // and every backend it spawned. See the module docs.
            if libc::setpgid(0, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }

            #[cfg(target_os = "linux")]
            {
                // The one layer macOS cannot have: the kernel signals us when the app goes,
                // including the SIGKILL no exit handler of ours survives.
                // Cast, because `prctl` is variadic and the kernel reads this argument as
                // an `unsigned long`. An `int` passed through varargs is a promotion the
                // ABI does not owe us.
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                // `prctl` is not atomic with the fork: if the app died in the window
                // between the two, the signal it was supposed to deliver has already been
                // missed and this process would run on forever holding credentials.
                // Reparenting is how that is visible from here.
                if libc::getppid() != parent {
                    libc::_exit(1);
                }
            }
            // `parent` is only read on Linux; naming it keeps the closure's capture honest
            // on the platforms that do not.
            let _ = parent;
            Ok(())
        });
    }

    let child = command.spawn()?;

    // Windows has no process group to put it in, so the job object stands in for both that
    // and the pidfile. See the module docs.
    #[cfg(windows)]
    job::adopt(&child);

    Ok(child)
}

/// A child's Job Object: what Windows has instead of a process group *and* instead of the
/// pidfile. See the module docs.
///
/// **One job per spawned child**, which is what makes `terminate` mean on Windows what it
/// means on Unix: that child and its descendants, and nothing else. It used to be one job
/// for the whole process, and the difference is invisible in the app -- which spawns one
/// gateway -- but not under `cargo test`, where the three integration tests run as threads
/// of a single binary. There, `terminate_all` in the teardown test reached into the other
/// two and killed the daemons they were about to connect to; both failed with a connection
/// refused whose cause was three stack frames away in another test. A supervisor primitive
/// whose blast radius is "every child this process ever started" is one that can only be
/// used once per process, and nothing said so.
///
/// The handle is kept in `JOBS` for the child's lifetime rather than closed after the
/// assignment, because the property that matters is tied to it: `KILL_ON_JOB_CLOSE` means
/// the kernel kills the job's members when its last handle closes, so holding the handle is
/// how an app that dies without running its teardown still takes its gateway with it.
///
/// The assignment happens just after `CreateProcess` rather than as part of it -- tokio
/// does not expose `CREATE_SUSPENDED` -- so there is a window, measured in the time it
/// takes a cold Python to reach its first `import`, in which a grandchild could escape the
/// job. The gateway spawns no backend that early; nothing here can close the window
/// without spawning the process by hand.
#[cfg(windows)]
mod job {
    use std::collections::HashMap;
    use std::sync::{Mutex, OnceLock};

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// Live jobs, by the pid of the child each one was made for. Handles are stored as
    /// `isize` because a raw handle is a pointer and the map has to be `Send`.
    ///
    /// A child that exits on its own without `terminate` leaves its entry here: the handle
    /// is a few bytes and the app spawns one gateway, so a reaper would be more moving
    /// parts than the leak it prevents.
    fn jobs() -> &'static Mutex<HashMap<u32, isize>> {
        // `OnceLock` rather than a plain `static`: `HashMap::new` is not a `const fn`, so
        // it cannot initialise one.
        static JOBS: OnceLock<Mutex<HashMap<u32, isize>>> = OnceLock::new();
        JOBS.get_or_init(|| Mutex::new(HashMap::new()))
    }

    /// A fresh job with the kill-on-close limit set, or `None` if the OS refused.
    ///
    /// Not fatal when it fails: `kill_on_drop` and the explicit `child.kill()` still run,
    /// and a shell that can start the gateway is more use than one that refuses to because
    /// a handle failed.
    fn create() -> Option<HANDLE> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                std::ptr::addr_of!(info).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                // Without the limit the job buys nothing the explicit kill does not, and a
                // handle that is never closed is worth closing here.
                CloseHandle(job);
                return None;
            }
            Some(job)
        }
    }

    /// Put a freshly spawned child in a job of its own, with everything it goes on to spawn.
    pub fn adopt(child: &tokio::process::Child) {
        let (Some(pid), Some(process)) = (child.id(), child.raw_handle()) else {
            return;
        };
        let Some(job) = create() else { return };
        unsafe {
            if AssignProcessToJobObject(job, process as HANDLE) == 0 {
                CloseHandle(job);
                return;
            }
        }
        // Replacing an entry would strand the old handle, and a stranded handle with
        // `KILL_ON_JOB_CLOSE` set is not a leak -- closing it kills whatever is still in
        // that job. Windows reuses pids, so close it deliberately rather than assume the
        // collision cannot happen.
        if let Some(stale) = jobs().lock().unwrap().insert(pid, job as isize) {
            unsafe {
                CloseHandle(stale as HANDLE);
            }
        }
    }

    /// Kill one child's job: that daemon and every backend under it, and nothing else.
    pub fn terminate_for(pid: u32) {
        let Some(job) = jobs().lock().unwrap().remove(&pid) else {
            return;
        };
        unsafe {
            TerminateJobObject(job as HANDLE, 1);
            CloseHandle(job as HANDLE);
        }
    }
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

/// The same promise on Windows, kept by the job object: `TerminateJobObject` reaches the
/// daemon and every backend under it, where `child.kill()` alone would reach only the
/// daemon and leave the `npx` processes holding credentials.
///
/// `this` child's job, not every job: see the `job` module on why that distinction is the
/// whole of the difference between the two platforms behaving alike and the integration
/// tests killing each other's daemons.
///
/// No grace period, because there is nothing to be polite with: Windows has no `SIGTERM`,
/// and the daemon's clean-shutdown handler is not reachable from another process.
#[cfg(windows)]
pub async fn terminate(child: &mut Child) {
    if let Some(pid) = child.id() {
        job::terminate_for(pid);
    }
    let _ = child.kill().await;
    let _ = child.wait().await;
}

#[cfg(not(any(unix, windows)))]
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

    /// `0` is what `settings::Connection::default()` carries, and what this app passed for
    /// its whole life before that file existed. The port is now a parameter -- somebody who
    /// wants to point a client at this app's own daemon needs a number that survives a
    /// restart -- so this pins the default rather than the only possibility.
    #[test]
    fn the_child_is_told_to_let_the_os_pick_the_port_unless_asked_otherwise() {
        let args = argv(
            Path::new("/data/servers.yaml"),
            Path::new("/data/gateway.port"),
            0,
        );
        let port = args.iter().position(|a| a == "--port").expect("--port");
        assert_eq!(
            args[port + 1],
            "0",
            "a fixed port by default would collide with `make run`"
        );

        let pinned = argv(
            Path::new("/data/servers.yaml"),
            Path::new("/data/gateway.port"),
            8765,
        );
        let port = pinned.iter().position(|a| a == "--port").expect("--port");
        assert_eq!(pinned[port + 1], "8765");
    }

    #[test]
    fn the_child_is_told_where_its_config_is_and_not_where_its_secrets_are() {
        let args = argv(
            Path::new("/data/servers.yaml"),
            Path::new("/data/gateway.port"),
            0,
        );
        assert!(args.contains(&"/data/servers.yaml".to_string()));
        // `cli.default_env_path` finds `gateway.env` beside the config. Passing `--env`
        // would be a second path to keep in sync with the seeding.
        assert!(!args.iter().any(|a| a == "--env"));
    }

    #[test]
    fn the_key_travels_in_the_environment_and_never_in_argv() {
        let key = "a-secret-that-must-not-be-public";
        let args = argv(
            Path::new("/data/servers.yaml"),
            Path::new("/data/gateway.port"),
            0,
        );
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
        // python-build-standalone puts the Windows interpreter at the root of the tree,
        // not under `bin/`. `bundle_python.py` knows the same thing; if one of them is
        // wrong the app starts nothing at all.
        #[cfg(windows)]
        assert!(layout.interpreter().ends_with("python\\python.exe"));
        assert!(layout.config().ends_with("servers.yaml"));
        assert!(layout.pidfile().ends_with("gateway.pid"));
    }

    /// The property the integration tests lost when the job was process-wide.
    ///
    /// Windows-only because there is nothing to test elsewhere: `terminate` on Unix has
    /// always been scoped to one process group. It needs no bundled interpreter, so it runs
    /// in every Windows leg rather than only the ones that ran `make tauri-python` -- which
    /// matters, because this is a bug that neither a Mac nor a Linux developer can reproduce
    /// and CI is therefore the only place it can ever be caught.
    #[cfg(windows)]
    #[tokio::test]
    async fn terminating_one_child_does_not_reach_another() {
        use std::time::Duration;

        // `ping` as a sleep: every Windows install has it, and a child that outlives the
        // assertions by 20 seconds is a child whose survival means something.
        fn sleeper() -> Child {
            tokio::process::Command::new("cmd")
                .args(["/c", "ping", "-n", "20", "127.0.0.1"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .spawn()
                .expect("cmd.exe should start")
        }

        let mut doomed = sleeper();
        let mut bystander = sleeper();
        job::adopt(&doomed);
        job::adopt(&bystander);

        terminate(&mut doomed).await;
        // A moment, so that "still running" is a statement about the kernel having had the
        // chance to kill it rather than about this thread being quick.
        tokio::time::sleep(Duration::from_millis(250)).await;

        assert!(
            bystander.try_wait().expect("wait should work").is_none(),
            "terminating one child killed another: the job is shared again, and the \
             integration tests will kill each other's daemons"
        );

        terminate(&mut bystander).await;
    }
}
