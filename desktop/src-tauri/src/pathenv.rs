//! What `PATH` the gateway's child processes get, and why it is not the one we inherited.
//!
//! An app launched from Finder inherits launchd's `PATH`, which is
//! `/usr/bin:/bin:/usr/sbin:/sbin` and nothing else. Every backend in a typical
//! `servers.yaml` is `npx -y @modelcontextprotocol/server-something`, and `npx` is in
//! Homebrew's prefix, or nvm's, or mise's -- none of which are on that list. So every
//! backend fails to spawn, `backend.py` reports a command that is not there, and the user,
//! who has `npx` in every terminal they have ever opened, concludes the app is broken.
//!
//! This is invisible in development: `cargo tauri dev` inherits the terminal's `PATH` and
//! the bug cannot reproduce. It only appears on a double-click, which is why
//! `desktop/README.md` puts "launch it from Finder" in the manual checklist.
//!
//! The fix is to ask the user's login shell what `PATH` is, once, at startup.
//! `backend.py`'s environment allowlist forwards the daemon's `PATH` to every backend, so
//! repairing it here repairs it everywhere -- there is one place to get this right.
//!
//! The `launchd` plist at `scripts/com.dbuschman7.mcp-gateway.plist` hit the same problem
//! and solved it with a hard-coded list. That list is [`FALLBACK`] below. Two copies of it
//! is exactly how one of them ends up missing `/opt/homebrew/bin`, so each names the other.

use std::time::Duration;

/// Where to look when the login shell cannot be asked.
///
/// The same list `scripts/com.dbuschman7.mcp-gateway.plist` sets, for the same reason. Keep
/// them together.
pub const FALLBACK: &str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

/// What launchd hands a Finder-launched app. Anything this small is a `PATH` that was
/// inherited rather than configured, and is the signal to go asking.
const MINIMAL: &[&str] = &["/usr/bin", "/bin", "/usr/sbin", "/sbin"];

/// How long the login shell gets. An interactive shell sources the user's rc files, which
/// is where the `PATH` actually is -- and also where a `read` at startup could hang
/// forever. Three seconds is far more than a shell needs and far less than a person waits.
const SHELL_TIMEOUT: Duration = Duration::from_secs(3);

/// Whether a `PATH` looks like one a GUI launch inherited rather than one a user configured.
///
/// Used to decide whether the login-shell probe is worth running at all: in development the
/// inherited `PATH` is the developer's own and is better than anything a subshell would
/// report.
pub fn looks_inherited(path: &str) -> bool {
    path.split(':')
        .filter(|entry| !entry.is_empty())
        .all(|entry| MINIMAL.contains(&entry))
}

/// Accept the login shell's answer, or explain why not.
///
/// Separated from running the shell so the decision is testable without spawning one. Every
/// rejection here is a real shape seen in the wild: a shell that printed nothing, a shell
/// that printed a banner instead of a `PATH`, an rc file that `echo`'d something with a NUL
/// in it.
pub fn accept(reported: &str, inherited: &str) -> Option<String> {
    let candidate = reported.trim().trim_end_matches('\n');
    if candidate.is_empty() || candidate.contains('\0') || candidate.contains('\n') {
        return None;
    }
    if !candidate.contains('/') {
        return None;
    }
    // Only take it if it is actually an improvement. A login shell that reports the same
    // minimal list has told us nothing, and replacing a good inherited PATH with a worse
    // one is the failure mode this guard exists for.
    if looks_inherited(candidate) && !looks_inherited(inherited) {
        return None;
    }
    Some(candidate.to_string())
}

/// The `PATH` to give the gateway, asking the login shell if the inherited one looks bare.
pub fn resolve() -> String {
    let inherited = std::env::var("PATH").unwrap_or_default();
    if !looks_inherited(&inherited) {
        return inherited;
    }
    match ask_login_shell() {
        Some(reported) => accept(&reported, &inherited).unwrap_or_else(|| fallback(&inherited)),
        None => fallback(&inherited),
    }
}

/// The inherited `PATH` widened by the known-good list, never narrowed.
fn fallback(inherited: &str) -> String {
    let mut entries: Vec<&str> = FALLBACK.split(':').collect();
    for entry in inherited.split(':').filter(|e| !e.is_empty()) {
        if !entries.contains(&entry) {
            entries.push(entry);
        }
    }
    entries.join(":")
}

/// Run `$SHELL -ilc 'printf %s "$PATH"'`, or `None` if that cannot be done in time.
///
/// `-i` because in zsh the `PATH` is usually set in `.zshrc`, which a non-interactive shell
/// does not read. `command printf` rather than `echo`, so a user's own `printf` function
/// cannot change the answer. stdin is closed, so an rc file that reads from it gets EOF
/// rather than blocking until the timeout.
#[cfg(unix)]
fn ask_login_shell() -> Option<String> {
    use std::io::Read;
    use std::process::{Command, Stdio};

    let shell = std::env::var("SHELL").ok()?;
    let mut child = Command::new(shell)
        .args(["-ilc", r#"command printf %s "$PATH""#])
        .env("TERM", "dumb")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    let deadline = std::time::Instant::now() + SHELL_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }

    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    Some(out)
}

#[cfg(not(unix))]
fn ask_login_shell() -> Option<String> {
    // Windows takes its PATH from the registry and a GUI process gets the same one a
    // console process does, so there is nothing to repair. Cross-platform support is its
    // own bead; this is here so the module compiles when that bead starts.
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    const FINDER: &str = "/usr/bin:/bin:/usr/sbin:/sbin";
    const TERMINAL: &str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";

    #[test]
    fn the_finder_path_is_recognised_as_inherited() {
        assert!(looks_inherited(FINDER));
        // Order and duplicates do not change what it is.
        assert!(looks_inherited("/bin:/usr/bin"));
        assert!(looks_inherited(""));
    }

    #[test]
    fn a_configured_path_is_left_alone() {
        assert!(!looks_inherited(TERMINAL));
        assert!(!looks_inherited("/usr/bin:/bin:/Users/someone/.local/bin"));
    }

    #[test]
    fn a_real_answer_from_the_login_shell_is_taken() {
        assert_eq!(accept(TERMINAL, FINDER).as_deref(), Some(TERMINAL));
        // Shells add a trailing newline; `printf %s` should not, but `echo` in a wrapper
        // would, and the answer is still good.
        assert_eq!(
            accept(&format!("{TERMINAL}\n"), FINDER).as_deref(),
            Some(TERMINAL)
        );
    }

    #[test]
    fn nothing_useful_is_refused_rather_than_used() {
        assert_eq!(accept("", FINDER), None);
        assert_eq!(accept("   \n  ", FINDER), None);
        // An rc file that printed a banner before the PATH.
        assert_eq!(accept("Welcome!\n/usr/bin:/bin", FINDER), None);
        // Something with a NUL in it is not a PATH.
        assert_eq!(accept("/usr/bin:\0:/bin", FINDER), None);
        // A word, not a path list.
        assert_eq!(accept("command not found", FINDER), None);
    }

    #[test]
    fn a_shell_that_reports_the_same_bare_path_has_told_us_nothing() {
        // ...but only when what we already had was better. If both are bare, taking the
        // shell's answer costs nothing and the fallback still runs behind it.
        assert_eq!(accept(FINDER, TERMINAL), None);
        assert_eq!(accept(FINDER, FINDER).as_deref(), Some(FINDER));
    }

    #[test]
    fn the_fallback_widens_the_inherited_path_and_never_narrows_it() {
        let widened = fallback("/usr/bin:/bin:/Users/someone/.cargo/bin");
        assert!(widened.contains("/opt/homebrew/bin"), "{widened}");
        // Whatever we were launched with survives, at the back.
        assert!(widened.contains("/Users/someone/.cargo/bin"), "{widened}");
        // No entry appears twice.
        let entries: Vec<&str> = widened.split(':').collect();
        let mut unique = entries.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(entries.len(), unique.len(), "{widened}");
    }

    #[test]
    fn the_fallback_matches_the_launchd_plist() {
        // Both files solve the same problem and must not drift. If this fails, one of them
        // was edited alone -- see the module docs.
        assert_eq!(
            FALLBACK,
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        );
    }
}
