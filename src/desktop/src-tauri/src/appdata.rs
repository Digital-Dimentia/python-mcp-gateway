//! The app's data directory: `servers.yaml`, `gateway.env`, and the rules about touching them.
//!
//! Both files are the *user's*, not the app's. `servers.yaml` is seeded once and then
//! belongs to whoever edits it -- by hand, or through the admin UI, which writes it via
//! `config_writer.py` with an atomic replace and a `.bak`. A second writer would be a
//! data-loss bug, so this module creates and never modifies.
//!
//! `gateway.env` holds every credential on the machine, which is the whole premise of the
//! project. It is created `0600` and, unlike the config, its mode *is* repaired on every
//! launch: `secrets.py` only warns about a group-readable store, and the app that created
//! the directory is in a position to do better than warn.
//!
//! That repair is Unix-only, because the mode bits are. On Windows the data directory is
//! under `%APPDATA%`, whose inherited ACL already grants the owning user and nobody else,
//! and rewriting it with a hand-built DACL would be a good deal more likely to lock a user
//! out of their own credentials than to protect them from anything. The seeding, the
//! pidfile and the rest of this module are the same everywhere.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

/// The credential store, and the directory holding it. Nothing else may read either.
#[cfg(unix)]
const PRIVATE_FILE: u32 = 0o600;
#[cfg(unix)]
const PRIVATE_DIR: u32 = 0o700;

/// What a first launch creates, and where each one comes from in the bundle.
pub const SEEDED: &[(&str, &str)] = &[
    ("servers.yaml", "seed/servers.yaml"),
    ("gateway.env", "seed/gateway.env.template"),
];

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Seeded {
    /// Files this launch created. Empty on every launch after the first.
    pub created: Vec<String>,
    /// Files whose permissions this launch tightened.
    pub tightened: Vec<String>,
}

/// Whether a mode lets anyone but the owner read the file.
#[cfg(unix)]
pub fn is_exposed(mode: u32) -> bool {
    mode & 0o077 != 0
}

/// Create the data directory and whatever is missing inside it.
///
/// Idempotent, and deliberately so: it runs on every launch, not just the first, because
/// the mode repair has to. A file that is already there is never read, rewritten, or
/// merged into -- only its permissions are looked at.
pub fn ensure(data_dir: &Path, seed_dir: &Path) -> io::Result<Seeded> {
    fs::create_dir_all(data_dir)?;
    #[cfg(unix)]
    fs::set_permissions(data_dir, fs::Permissions::from_mode(PRIVATE_DIR))?;

    let mut report = Seeded::default();
    for (name, source) in SEEDED {
        let target = data_dir.join(name);
        if !target.exists() {
            let from = seed_dir.join(Path::new(source).file_name().unwrap());
            fs::copy(&from, &target)?;
            report.created.push((*name).to_string());
        }
        #[cfg(unix)]
        if *name == "gateway.env" {
            let mode = fs::metadata(&target)?.permissions().mode() & 0o777;
            if is_exposed(mode) {
                fs::set_permissions(&target, fs::Permissions::from_mode(PRIVATE_FILE))?;
                report.tightened.push((*name).to_string());
            }
        }
    }
    Ok(report)
}

/// Record this process's pid, so a launch after a Force Quit can find what it left behind.
pub fn write_pidfile(path: &Path, pid: u32) -> io::Result<()> {
    fs::write(path, format!("{pid}\n"))
}

/// The pid a previous run left, if the file names one.
pub fn read_pidfile(path: &Path) -> Option<u32> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

pub fn clear_pidfile(path: &Path) {
    let _ = fs::remove_file(path);
}

/// How long a child gets to go quietly before it is killed, and how often we look.
///
/// Short, because this runs while an app is quitting and a person is watching the window
/// not close. The daemon's own shutdown is a socket close and a wait on its backends, which
/// is fast when nothing is wedged -- and when something *is* wedged, `SIGKILL` on the group
/// is the right answer rather than a longer wait.
#[cfg(unix)]
const GRACE: std::time::Duration = std::time::Duration::from_millis(800);
#[cfg(unix)]
const POLL: std::time::Duration = std::time::Duration::from_millis(20);

/// Stop whatever a pidfile names, and clear the record **only once it is gone**.
///
/// The ordering is the whole point, and getting it backwards is what left seventeen
/// orphaned daemons on a developer's machine (python-mcp-gateway-g90.10). A pidfile is not
/// bookkeeping: it is the one record that lets the *next* launch find a child this one
/// failed to stop. Clearing it first -- which is what both callers used to do -- spends
/// that record on a process that may still be running, and then nothing anywhere knows the
/// child exists.
///
/// The pid check is not optional either. Pids are reused, and killing a stranger's process
/// because it inherited a number is far worse than leaving a stale one, so nothing is
/// signalled unless its executable is the program we would have started.
///
/// `SIGTERM` to the **process group**, because `spawn_hardened` gave the child its own and
/// its backends live in it: signalling the pid alone would stop the daemon and leave the
/// servers it spawned. Then `SIGKILL` to the same group if it is still there, because an
/// app that has been told to quit has already stopped negotiating.
#[cfg(unix)]
pub fn stop_recorded(pidfile: &Path, program: &Path) -> Option<u32> {
    let pid = read_pidfile(pidfile)?;
    // 0 is "every process in our group" and 1 is init. Neither is a child of ours, and
    // `kill(-0, ...)` would signal this process -- so the guard is load-bearing, not a
    // sanity check.
    if pid <= 1 {
        clear_pidfile(pidfile);
        return None;
    }
    if !is_ours(pid, program) {
        // The number was reused, or the child is already gone. Either way the record names
        // nothing of ours and keeping it would make the next launch signal a stranger.
        clear_pidfile(pidfile);
        return None;
    }
    signal_group(pid, libc::SIGTERM);
    if !gone_within(pid, program, GRACE) {
        signal_group(pid, libc::SIGKILL);
        // A short second look rather than none: `SIGKILL` is not instantaneous, and the
        // answer to "did it work" decides whether the record survives this function.
        gone_within(pid, program, GRACE / 4);
    }
    if is_ours(pid, program) {
        // Still there. Leave the pidfile: the next launch is now the only thing that can
        // find this process, and a record pointing at a live child is exactly what it is
        // for. This is the branch that must never be traded for a tidier data directory.
        return None;
    }
    clear_pidfile(pidfile);
    Some(pid)
}

#[cfg(unix)]
fn signal_group(pid: u32, signal: libc::c_int) {
    // The negative pid is the group. See `supervisor`'s module docs for why the child has
    // one of its own.
    unsafe {
        libc::kill(-(pid as libc::pid_t), signal);
    }
}

/// Whether `pid` stops being one of ours within `limit`.
#[cfg(unix)]
fn gone_within(pid: u32, program: &Path, limit: std::time::Duration) -> bool {
    let deadline = std::time::Instant::now() + limit;
    loop {
        if !is_ours(pid, program) {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(POLL);
    }
}

/// Kill a gateway a previous run left behind, if the pid still names one of ours.
///
/// The launch-time half of `stop_recorded`, and the same function underneath: what a
/// previous run failed to stop is exactly what this run has to stop, and two
/// implementations of "kill what the pidfile names" would be two things to keep in step.
#[cfg(unix)]
pub fn reap_previous(pidfile: &Path, interpreter: &Path) -> Option<u32> {
    stop_recorded(pidfile, interpreter)
}

/// Whether `pid` is running *our* interpreter, rather than whatever reused the number.
///
/// Linux answers this exactly, macOS approximately, and the difference is not a detail:
/// `ps -o comm=` prints the full executable path on macOS and only the first fifteen
/// characters of the *name* on Linux, so the macOS test applied there would compare
/// `python3` against an absolute path, never match, and quietly turn the reaper off.
#[cfg(target_os = "linux")]
fn is_ours(pid: u32, interpreter: &Path) -> bool {
    // `/proc/<pid>/exe` is the kernel's own answer, with no truncation and no parsing.
    match fs::read_link(format!("/proc/{pid}/exe")) {
        Ok(exe) => exe == interpreter || fs::canonicalize(interpreter).is_ok_and(|c| exe == c),
        Err(_) => false,
    }
}

#[cfg(all(unix, not(target_os = "linux")))]
fn is_ours(pid: u32, interpreter: &Path) -> bool {
    let output = std::process::Command::new("/bin/ps")
        .args(["-o", "comm=", "-p", &pid.to_string()])
        .output();
    match output {
        Ok(out) if out.status.success() => {
            let comm = String::from_utf8_lossy(&out.stdout);
            let comm = comm.trim();
            !comm.is_empty() && Path::new(comm) == interpreter
        }
        _ => false,
    }
}

/// Nothing to reap on Windows, and deliberately so.
///
/// The pidfile exists for the one case signals and `kill_on_drop` cannot reach: the shell
/// being killed outright. Windows answers that in the kernel instead -- every gateway is a
/// member of a Job Object with `KILL_ON_JOB_CLOSE`, so when this process dies by any means
/// the job's last handle closes and its members go with it. There is never a gateway left
/// behind to find, and a pidfile hunt here would only be able to find the wrong process.
/// See `supervisor::job`.
#[cfg(not(unix))]
pub fn reap_previous(_pidfile: &Path, _interpreter: &Path) -> Option<u32> {
    None
}

/// Windows has no process groups to signal and no `/proc` to identify a pid with. The Job
/// Object `supervisor` puts the child in is the mechanism there: closing the job's last
/// handle -- which happens when this process dies, however it dies -- kills everything in
/// it. That is the pidfile layer done by the kernel, so there is nothing to do here.
#[cfg(not(unix))]
pub fn stop_recorded(_pidfile: &Path, _program: &Path) -> Option<u32> {
    None
}

/// Where the seed files live inside the bundle.
pub fn seed_dir(resource_dir: &Path) -> PathBuf {
    resource_dir.join("seed")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A child in its own process group with a grandchild in it, like the daemon and its
    /// backends. `sh` is the stand-in, because the point is the group rather than Python.
    #[cfg(unix)]
    fn a_child_with_a_child_of_its_own() -> (std::process::Child, u32) {
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new("/bin/sh");
        command.args(["-c", "sleep 300 & sleep 300"]);
        unsafe {
            command.pre_exec(|| {
                // What `supervisor::spawn_hardened` does, and the reason a group signal
                // reaches the backends a daemon spawned.
                if libc::setpgid(0, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = command.spawn().expect("a child");
        let pid = child.id();
        // Give `sh` a moment to fork the one that has to die with it.
        std::thread::sleep(std::time::Duration::from_millis(100));
        (child, pid)
    }

    #[cfg(unix)]
    fn alive(pid: u32) -> bool {
        unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
    }

    #[cfg(unix)]
    #[test]
    fn stopping_what_the_pidfile_names_kills_the_whole_group_and_clears_the_record() {
        let root = scratch("stop-recorded");
        let pidfile = root.join("gateway.pid");
        let (mut child, pid) = a_child_with_a_child_of_its_own();
        write_pidfile(&pidfile, pid).expect("a record");

        let stopped = stop_recorded(&pidfile, Path::new("/bin/sh"));

        assert_eq!(stopped, Some(pid));
        assert!(!pidfile.exists(), "the record goes only once the child has");
        // Reaped so the pid does not linger as a zombie, which `kill(pid, 0)` still answers
        // for -- the assertion below would pass for the wrong reason without this.
        let _ = child.wait();
        assert!(!alive(pid));
        // And the grandchildren with it, which is the whole reason the signal goes to the
        // group: a daemon's backends are its children, and stopping the daemon alone would
        // leave every MCP server it spawned running. `pgrep -g` lists a process group.
        let survivors = std::process::Command::new("/usr/bin/pgrep")
            .args(["-g", &pid.to_string()])
            .output()
            .expect("pgrep runs");
        assert!(
            String::from_utf8_lossy(&survivors.stdout).trim().is_empty(),
            "the group still has members: {}",
            String::from_utf8_lossy(&survivors.stdout)
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_pid_that_is_not_ours_is_never_signalled() {
        // The pid of *this* test process, which is emphatically not `/bin/sleep`. A version
        // that signalled first and identified afterwards would kill the test suite, which
        // is a memorable way to find out.
        let root = scratch("stop-stranger");
        let pidfile = root.join("gateway.pid");
        write_pidfile(&pidfile, std::process::id()).expect("a record");

        assert_eq!(stop_recorded(&pidfile, Path::new("/bin/sleep")), None);
        assert!(
            !pidfile.exists(),
            "a record naming a stranger is not worth keeping"
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_record_naming_nothing_dangerous_is_dropped_rather_than_signalled() {
        // `kill(-0, ...)` signals *this* process's group, which in a test runner is the
        // test runner. Hence the guard, and hence this test.
        let root = scratch("stop-zero");
        for pid in [0u32, 1] {
            let pidfile = root.join(format!("{pid}.pid"));
            write_pidfile(&pidfile, pid).expect("a record");
            assert_eq!(stop_recorded(&pidfile, Path::new("/bin/sleep")), None);
            assert!(!pidfile.exists());
        }
    }

    #[cfg(unix)]
    #[test]
    fn nothing_recorded_is_nothing_to_do() {
        let root = scratch("stop-empty");
        assert_eq!(
            stop_recorded(&root.join("absent.pid"), Path::new("/bin/sleep")),
            None
        );
    }

    /// A stand-in for the bundle's `seed/` directory.
    fn seed(root: &Path) -> PathBuf {
        let dir = root.join("seed");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("servers.yaml"), "version: 1\nservers: {}\n").unwrap();
        fs::write(dir.join("gateway.env.template"), "# no values here\n").unwrap();
        dir
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("mcp-gateway-appdata-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_first_launch_creates_both_files() {
        let root = scratch("first");
        let seed_dir = seed(&root);
        let data = root.join("data");

        let report = ensure(&data, &seed_dir).unwrap();

        assert_eq!(report.created, vec!["servers.yaml", "gateway.env"]);
        assert!(data.join("servers.yaml").exists());
        assert!(data.join("gateway.env").exists());
    }

    #[test]
    fn a_second_launch_creates_nothing_and_changes_nothing() {
        let root = scratch("second");
        let seed_dir = seed(&root);
        let data = root.join("data");

        ensure(&data, &seed_dir).unwrap();
        // The user's own config, which the app must never touch again.
        let mine = "version: 1\nservers:\n  github:\n    command: npx\n";
        fs::write(data.join("servers.yaml"), mine).unwrap();

        let report = ensure(&data, &seed_dir).unwrap();

        assert!(report.created.is_empty());
        assert_eq!(fs::read_to_string(data.join("servers.yaml")).unwrap(), mine);
    }

    #[cfg(unix)]
    #[test]
    fn the_credential_store_is_private_when_created() {
        let root = scratch("mode");
        let seed_dir = seed(&root);
        let data = root.join("data");

        ensure(&data, &seed_dir).unwrap();

        let mode = fs::metadata(data.join("gateway.env"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert!(!is_exposed(mode), "mode {mode:o}");
        let dir_mode = fs::metadata(&data).unwrap().permissions().mode() & 0o777;
        assert!(!is_exposed(dir_mode), "mode {dir_mode:o}");
    }

    #[cfg(unix)]
    #[test]
    fn a_loosened_credential_store_is_tightened_on_the_next_launch() {
        let root = scratch("tighten");
        let seed_dir = seed(&root);
        let data = root.join("data");

        ensure(&data, &seed_dir).unwrap();
        fs::set_permissions(data.join("gateway.env"), fs::Permissions::from_mode(0o644)).unwrap();

        let report = ensure(&data, &seed_dir).unwrap();

        assert_eq!(report.tightened, vec!["gateway.env"]);
        let mode = fs::metadata(data.join("gateway.env"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, PRIVATE_FILE);
    }

    #[cfg(unix)]
    #[test]
    fn the_config_is_not_tightened_because_it_holds_no_secrets() {
        let root = scratch("config-mode");
        let seed_dir = seed(&root);
        let data = root.join("data");

        ensure(&data, &seed_dir).unwrap();
        fs::set_permissions(data.join("servers.yaml"), fs::Permissions::from_mode(0o644)).unwrap();

        let report = ensure(&data, &seed_dir).unwrap();

        // `servers.yaml` is committed in this very repository. It names `${VAR}` and never
        // a value; there is nothing here to protect.
        assert!(report.tightened.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn exposure_is_about_anyone_but_the_owner() {
        assert!(!is_exposed(0o600));
        assert!(!is_exposed(0o400));
        assert!(is_exposed(0o640), "the group can read it");
        assert!(is_exposed(0o604), "everyone can read it");
        assert!(is_exposed(0o666));
    }

    #[test]
    fn a_pidfile_round_trips_and_a_broken_one_is_simply_absent() {
        let root = scratch("pid");
        let path = root.join("gateway.pid");

        assert_eq!(read_pidfile(&path), None, "no file");
        write_pidfile(&path, 4242).unwrap();
        assert_eq!(read_pidfile(&path), Some(4242));

        fs::write(&path, "not a pid\n").unwrap();
        assert_eq!(read_pidfile(&path), None);

        clear_pidfile(&path);
        assert!(!path.exists());
    }

    #[cfg(unix)]
    #[test]
    fn a_pid_that_is_not_running_our_interpreter_is_left_alone() {
        let root = scratch("reap");
        let pidfile = root.join("gateway.pid");
        // pid 1 is launchd. If the guard were only "is this pid alive", this is what the
        // app would signal after a reused pid.
        write_pidfile(&pidfile, 1).unwrap();
        assert_eq!(
            reap_previous(&pidfile, Path::new("/nonexistent/python3")),
            None
        );
    }
}
