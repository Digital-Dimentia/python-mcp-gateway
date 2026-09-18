//! The SSH port forward: how the window reaches a gateway on another machine.
//!
//! One `ssh -N -L` child, supervised the way `supervisor.rs` supervises the daemon — and
//! that symmetry is the whole design. A forwarded loopback port is indistinguishable from a
//! local one at the socket layer, so `proxy.rs` keeps dialling `ws://127.0.0.1:<port>` and
//! nothing downstream of it knows or cares which machine answered. What changes is what is
//! being supervised, and whether an `Authorization` header goes on.
//!
//! ## Why the system `ssh` and not a Rust SSH library
//!
//! Because `~/.ssh/config` is the thing people already have. Aliases, `ProxyJump`, per-host
//! identities, `Match` blocks, hardware keys through the agent, a corporate CA — every one
//! of those works here for free and none of them would work in a library that reimplements
//! the client. It also means this app never handles a private key, a passphrase or a
//! password, which is what lets the settings file hold no credential at all.
//!
//! ## Why it can never prompt
//!
//! `BatchMode=yes`, and stdin is `Stdio::null()`. An app launched from Finder has no
//! terminal: an `ssh` that decided to ask for a password, or to ask whether you trust a new
//! host key, would block on a stdin nobody can type into, and the window would sit at
//! "connecting" forever with no way to find out why. Refusing to prompt turns "this needs a
//! human" into an exit code and a line on stderr — which is to say, into a sentence the
//! Connection screen can show, with the fix attached.
//!
//! The corollary is that the first connection to a host has to be made by hand, once, in a
//! terminal. That is a real cost and it is the right one: the alternative is
//! `StrictHostKeyChecking=accept-new`, which is this app silently vouching for a host key on
//! the user's behalf — on the single connection that is also its entire authentication
//! story. Do not add it.
//!
//! ## Why the local port is fixed
//!
//! `ssh -L 0:…` would let the OS pick, and the only way to learn what it picked is to read
//! `Allocated port 57113 for local forward` off stderr. That is the exact practice
//! `tests/test_desktop_layout.py::test_the_shell_no_longer_reads_the_port_off_a_log_line`
//! exists to forbid: a sentence written for a human is not a wire format. It is also not
//! what anyone wants — a client config saying `ws://127.0.0.1:57113/mcp` is wrong by
//! tomorrow morning. So the number comes from the settings, defaults to the daemon's own
//! 8765 at both ends, and stays put.
//!
//! stderr is still *read*, but only ever to classify a failure into one of a handful of
//! kinds. The worst case of a missed match is a vaguer message; the worst case of a missed
//! port is a socket aimed at a stranger.

use std::io;
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::process::{Child, Command};

/// How often the far side is probed while the tunnel is coming up.
pub const PROBE_INTERVAL: Duration = Duration::from_millis(250);

/// How long a probe waits for the loopback end of a forward. Generous by loopback standards
/// and still shorter than a person's patience: the connection is to this machine, and what
/// is slow is the round trip ssh makes to open the channel.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

/// The keepalive pair, as seconds and counts, in one place so the docs cannot drift from the
/// argv. Three missed fifteen-second probes is the sleep-and-wake case: a laptop that
/// changed networks leaves a forward that is dead but not closed, and every socket through
/// it hangs until something notices.
pub const ALIVE_INTERVAL: u32 = 15;
pub const ALIVE_COUNT: u32 = 3;

/// The `ssh` to run.
///
/// Absolute by preference, and for two reasons that are not style. It is the system client,
/// which is the one that reads `~/.ssh/config` and talks to the agent — a shim earlier on
/// `PATH` from some package manager may do neither. And `appdata::is_ours` identifies a
/// leftover process by comparing its executable against a path, so a tunnel spawned as a
/// bare name could not be reaped after a Force Quit.
pub fn program() -> PathBuf {
    for candidate in [
        "/usr/bin/ssh",
        r"C:\Windows\System32\OpenSSH\ssh.exe",
    ] {
        let path = PathBuf::from(candidate);
        if path.exists() {
            return path;
        }
    }
    PathBuf::from("ssh")
}

/// The argv for the forward. A function so every flag is visible in one place and testable.
///
/// The destination is **last**, and every option is its own `-o key=value` pair. `ssh` has
/// no `--` terminator, so there is no way to tell it "everything after this is a hostname";
/// position is the only defence there is, and `settings::Connection::validate` is the other
/// half of it.
pub fn argv(destination: &str, local_port: u16, remote_port: u16) -> Vec<String> {
    vec![
        // No remote command, and no pty to run one in. This process is a forward.
        "-N".into(),
        "-T".into(),
        // Without this, an `ssh` that cannot bind the local port connects anyway and logs a
        // warning: the app would sit there "connected" to a forward that does not exist.
        "-o".into(),
        "ExitOnForwardFailure=yes".into(),
        // See the module docs. This is what makes every failure a message.
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        format!("ServerAliveInterval={ALIVE_INTERVAL}"),
        "-o".into(),
        format!("ServerAliveCountMax={ALIVE_COUNT}"),
        // A host that is off or unroutable should be a failure in seconds, not in whatever
        // the kernel's SYN retry schedule adds up to.
        "-o".into(),
        "ConnectTimeout=10".into(),
        // Opting out of connection sharing, deliberately. If the user's config says
        // `ControlMaster auto`, this child could attach to — or become — a master whose
        // lifetime belongs to some other session: killing it might leave the forward up, and
        // someone else's logout might take ours down. Every layer of teardown in this app
        // rests on "this child's life is the tunnel's life", and multiplexing is the one
        // thing that can make that false.
        "-o".into(),
        "ControlMaster=no".into(),
        "-o".into(),
        "ControlPath=none".into(),
        // Both ends named explicitly. The near end is bound to loopback rather than left to
        // `GatewayPorts`, so the tunnel is reachable from this machine and nowhere else.
        "-L".into(),
        format!("127.0.0.1:{local_port}:127.0.0.1:{remote_port}"),
        destination.to_string(),
    ]
}

/// Why a tunnel is not up, in the kinds that want different things done about them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    /// ssh could not authenticate without asking, which under `BatchMode` it will not do.
    AuthRequired,
    /// The host key is unknown or has changed. A person has to look at this.
    HostKey,
    /// No such host, as far as DNS and `~/.ssh/config` are concerned.
    Unresolved,
    /// The near end of the forward is already taken on this machine.
    LocalPortBusy,
    /// The forward is open and the far side refused the connection: the tunnel works and the
    /// gateway over there is not running.
    RemoteForwardRefused,
    /// The network went away, the lid closed, the server stopped answering keepalives.
    NetworkTransient,
}

impl Fault {
    /// Whether retrying could possibly help.
    ///
    /// The split matters more here than it does for the daemon. A wrong host key retried
    /// five times is five identical failures and a worse message, so those stop at once. A
    /// closed laptop lid, though, is not a broken configuration — and an app that gave up
    /// permanently after forty seconds of it is an app you restart every morning. So the
    /// transient kinds retry for as long as the window is open.
    pub fn permanent(self) -> bool {
        matches!(
            self,
            Fault::AuthRequired | Fault::HostKey | Fault::Unresolved | Fault::LocalPortBusy
        )
    }

    /// What to tell the person, and what to tell them to do about it.
    ///
    /// The second half is the whole value. "Permission denied (publickey)" is a sentence
    /// most people have seen and few can act on from inside an app that has no terminal.
    pub fn hint(self) -> &'static str {
        match self {
            Fault::AuthRequired => {
                "SSH is the only authentication here, and this app never asks you for a \
                 password or a passphrase. Run `ssh <destination>` once in Terminal: if it \
                 works there — with your key loaded in the agent — it will work here."
            }
            Fault::HostKey => {
                "Run `ssh <destination>` once in Terminal and resolve it there. This app \
                 will not accept a host key on your behalf."
            }
            Fault::Unresolved => {
                "Check the name. Anything beyond a plain hostname — a login, a port, a jump \
                 host — belongs in ~/.ssh/config under a Host block whose alias you put in \
                 the destination field."
            }
            Fault::LocalPortBusy => {
                "Something on this Mac is already listening there — very likely a gateway \
                 running locally, or a tunnel you opened by hand. Stop it, or choose another \
                 local port and point your clients at that instead."
            }
            Fault::RemoteForwardRefused => {
                "The tunnel is open, so this is the gateway over there, not the connection. \
                 Start it on that machine: `systemctl --user start mcp-gateway`, or `make \
                 run` in a checkout."
            }
            Fault::NetworkTransient => {
                "This usually clears on its own — a sleeping laptop, a changed network. The \
                 tunnel keeps trying."
            }
        }
    }
}

/// Read one line of ssh's stderr as a kind of failure, or as nothing in particular.
///
/// Classification, never extraction: nothing here ever takes a *value* out of a log line.
/// See the module docs for why that distinction is a rule in this repository rather than a
/// preference.
pub fn classify(line: &str) -> Option<Fault> {
    let line = line.to_ascii_lowercase();
    let has = |needle: &str| line.contains(needle);

    if has("permission denied")
        || has("no supported authentication")
        || has("too many authentication failures")
        || has("host key verification failed") && has("permission denied")
    {
        return Some(Fault::AuthRequired);
    }
    if has("host key verification failed")
        || has("remote host identification has changed")
        || has("no matching host key type")
    {
        return Some(Fault::HostKey);
    }
    if has("could not resolve hostname")
        || has("name or service not known")
        || has("nodename nor servname provided")
    {
        return Some(Fault::Unresolved);
    }
    // "bind [127.0.0.1]:8765: Address already in use" -- the address is interpolated into
    // the middle of the sentence, so the match is on the tail of it.
    if has("address already in use")
        || has("cannot listen to port")
        || has("could not request local forwarding")
    {
        return Some(Fault::LocalPortBusy);
    }
    if has("open failed: connect failed") || has("administratively prohibited") {
        return Some(Fault::RemoteForwardRefused);
    }
    // "Operation timed out" on macOS, "Connection timed out" on Linux, and ssh's own
    // "Timeout, server X not responding" when the keepalives run out.
    if has("timed out")
        || has("connection refused")
        || has("connection closed by")
        || has("broken pipe")
        || has("not responding")
        || has("network is unreachable")
        || has("no route to host")
    {
        return Some(Fault::NetworkTransient);
    }
    None
}

/// What is on the near end of the forward.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Probe {
    /// Nothing accepted the connection: ssh has not bound the port yet.
    NoListener,
    /// Something accepted and then went away without a word. With `-L` that is what a failed
    /// channel looks like from here: ssh accepts locally the moment it has authenticated,
    /// and only *then* asks the far side to connect — so a tunnel to a machine where the
    /// gateway is not running accepts every connection and drops each one.
    ///
    /// This is precisely why a bare TCP connect is not a readiness check.
    FarSideRefused,
    /// A gateway answered.
    Alive,
}

/// Ask the near end of the forward what is behind it.
///
/// An HTTP request rather than a WebSocket handshake, because `/ui` needs no access key —
/// which means the probe works against a keyless remote daemon *and* against a keyed local
/// one, involves no secret, and costs the daemon nothing. Any status line proves a gateway:
/// even a 404 begins `HTTP/`.
pub async fn probe(port: u16, timeout: Duration) -> Probe {
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let attempt = tokio::time::timeout(timeout, async {
        let mut socket = TcpStream::connect(address).await?;
        socket
            .write_all(b"GET /ui/ HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            .await?;
        let mut head = [0_u8; 5];
        socket.read_exact(&mut head).await?;
        io::Result::Ok(head)
    })
    .await;

    match attempt {
        Ok(Ok(head)) if head.starts_with(b"HTTP/") => Probe::Alive,
        // Answered, but not like a gateway. Something else is on that port; treat it as the
        // far side being wrong rather than as nothing being there, because "nothing" would
        // have the supervisor wait patiently forever.
        Ok(Ok(_)) => Probe::FarSideRefused,
        Ok(Err(err)) if err.kind() == io::ErrorKind::ConnectionRefused => Probe::NoListener,
        // Accepted and then closed: the signature of a forward whose far end refused.
        Ok(Err(_)) => Probe::FarSideRefused,
        // The connect itself never finished. Nothing is listening yet, as far as we know.
        Err(_) => Probe::NoListener,
    }
}

/// Whether this machine can give the forward its near end, and what to say if it cannot.
///
/// A check whose whole purpose is the message. `ExitOnForwardFailure` is the authority and
/// this races it — something could take the port between here and `ssh` — but a bind that
/// fails *now* can name the problem in the Connection screen, where ssh's own
/// "bind: Address already in use" arrives as one line of a log a person has to go and read.
pub fn preflight(local_port: u16) -> Result<(), String> {
    match TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, local_port)) {
        Ok(listener) => {
            // Dropped at once: this was a question, not a reservation.
            drop(listener);
            Ok(())
        }
        Err(err) if err.kind() == io::ErrorKind::AddrInUse => Err(format!(
            "port {local_port} on this Mac is already in use. {}",
            Fault::LocalPortBusy.hint()
        )),
        Err(err) => Err(format!("port {local_port} cannot be opened on this Mac: {err}")),
    }
}

/// Spawn the forward.
///
/// `program` is a parameter rather than a call to `program()` so the tests can put a
/// stand-in on the other end of it and drive the failure paths without a second machine.
///
/// **The environment is inherited**, plus the repaired `PATH`, and this is a deliberate
/// difference from `supervisor::environment`, which hands the daemon exactly four variables.
/// `ssh` needs `HOME` to find `~/.ssh/config` and `SSH_AUTH_SOCK` to reach the agent, and
/// neither is ours to synthesise. It also means an app launched from Finder depends on
/// launchd having passed `SSH_AUTH_SOCK` down — the same family of problem `pathenv` exists
/// for, and the first thing to check when a tunnel works in `make tauri-dev` and not in the
/// bundled app.
pub fn spawn(
    program: &Path,
    destination: &str,
    local_port: u16,
    remote_port: u16,
    path: &str,
) -> io::Result<Child> {
    let mut command = Command::new(program);
    command
        .args(argv(destination, local_port, remote_port))
        .env("PATH", path)
        // Null, and load-bearing rather than tidy: it is the second half of `BatchMode`.
        // An ssh that found a reason to prompt would block here forever instead of failing.
        .stdin(Stdio::null())
        // `-N` writes nothing to stdout. Everything ssh has to say is on stderr.
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    crate::supervisor::spawn_hardened(&mut command)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flags(destination: &str) -> Vec<String> {
        argv(destination, 8765, 8765)
    }

    fn has_option(args: &[String], option: &str) -> bool {
        args.windows(2)
            .any(|pair| pair[0] == "-o" && pair[1] == option)
    }

    #[test]
    fn the_forward_names_loopback_at_both_ends() {
        let args = argv("build-box", 8765, 9000);
        let at = args.iter().position(|a| a == "-L").expect("-L");
        assert_eq!(args[at + 1], "127.0.0.1:8765:127.0.0.1:9000");
        // Never `*:8765:…` and never `8765:…`, either of which would put the tunnel on
        // every interface of this machine -- an unauthenticated gateway on the LAN, which
        // is the one thing the daemon refuses to do for itself.
        assert!(!args.iter().any(|a| a.starts_with('*')));
    }

    #[test]
    fn a_forward_that_cannot_bind_fails_instead_of_pretending() {
        assert!(has_option(&flags("build-box"), "ExitOnForwardFailure=yes"));
    }

    #[test]
    fn the_tunnel_never_asks_a_human_for_anything() {
        // With stdin null, a prompt is a hang -- see the module docs. This flag is what
        // turns every "needs a human" into an exit and a line we can classify.
        assert!(has_option(&flags("build-box"), "BatchMode=yes"));
    }

    #[test]
    fn the_tunnel_does_not_weaken_host_key_checking() {
        // The test that exists to stop a future quick fix. When someone hits
        // "Host key verification failed", the answer is the message in `Fault::HostKey`,
        // not `StrictHostKeyChecking=accept-new` -- which would be this app vouching for a
        // host key on the user's behalf, on the connection that *is* the authentication.
        assert!(
            !flags("build-box")
                .iter()
                .any(|a| a.contains("StrictHostKeyChecking")),
            "host key checking is the user's to decide, in their own terminal"
        );
    }

    #[test]
    fn the_tunnel_does_not_share_a_multiplexed_connection() {
        let args = flags("build-box");
        assert!(has_option(&args, "ControlMaster=no"));
        assert!(has_option(&args, "ControlPath=none"));
    }

    #[test]
    fn a_sleeping_laptop_is_noticed_rather_than_hung_on() {
        let args = flags("build-box");
        assert!(has_option(&args, "ServerAliveInterval=15"));
        assert!(has_option(&args, "ServerAliveCountMax=3"));
    }

    #[test]
    fn the_destination_is_the_last_word_and_nothing_follows_it() {
        // `ssh` has no `--`, so position is the defence. `settings::Connection::validate`
        // is the other half: together they are why a destination cannot become an option.
        let args = flags("dave@build-box");
        assert_eq!(args.last().unwrap(), "dave@build-box");
        assert_eq!(
            args.iter().filter(|a| *a == "dave@build-box").count(),
            1,
            "named once, at the end"
        );
    }

    #[test]
    fn the_tunnel_runs_no_remote_command() {
        let args = flags("build-box");
        assert!(args.contains(&"-N".to_string()));
        assert!(args.contains(&"-T".to_string()));
    }

    #[test]
    fn nothing_in_argv_is_a_secret() {
        // There is no secret to leak in remote mode -- SSH is the authentication and the
        // settings hold no credential -- and this is the test that notices if that changes.
        for arg in flags("dave@build-box") {
            let arg = arg.to_ascii_lowercase();
            for forbidden in ["key=", "password", "secret", "token", "bearer"] {
                assert!(!arg.contains(forbidden), "{arg} looks like a credential");
            }
        }
    }

    // --- reading ssh's stderr -----------------------------------------------------------

    #[test]
    fn authentication_that_needs_a_human_is_recognised() {
        for line in [
            "dave@build-box: Permission denied (publickey).",
            "Permission denied, please try again.",
            "Received disconnect from 10.0.0.4 port 22:2: Too many authentication failures",
            "No supported authentication methods available",
        ] {
            assert_eq!(classify(line), Some(Fault::AuthRequired), "{line}");
        }
    }

    #[test]
    fn a_host_key_problem_is_never_retried_away() {
        for line in [
            "Host key verification failed.",
            "@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@",
        ] {
            let fault = classify(line).expect(line);
            assert_eq!(fault, Fault::HostKey);
            assert!(fault.permanent(), "retrying cannot fix a host key");
        }
    }

    #[test]
    fn a_name_that_does_not_resolve_is_recognised() {
        for line in [
            "ssh: Could not resolve hostname buld-box: nodename nor servname provided, or not known",
            "ssh: Could not resolve hostname x: Name or service not known",
        ] {
            assert_eq!(classify(line), Some(Fault::Unresolved), "{line}");
        }
    }

    #[test]
    fn a_busy_local_port_is_recognised_and_is_not_the_remotes_fault() {
        for line in [
            "bind [127.0.0.1]:8765: Address already in use",
            "channel_setup_fwd_listener_tcpip: cannot listen to port: 8765",
            "Could not request local forwarding.",
        ] {
            assert_eq!(classify(line), Some(Fault::LocalPortBusy), "{line}");
        }
        assert!(Fault::LocalPortBusy.permanent(), "the port will not free itself");
    }

    #[test]
    fn a_silent_far_side_is_told_apart_from_a_broken_tunnel() {
        // The common case, and the one that reads as "the app is broken" unless it is named:
        // the tunnel is perfect and the gateway over there is simply not running.
        let fault = classify("channel 2: open failed: connect failed: Connection refused")
            .expect("a fault");
        assert_eq!(fault, Fault::RemoteForwardRefused);
        assert!(
            !fault.permanent(),
            "the daemon over there may well come back, and the tunnel is fine meanwhile"
        );
        assert!(fault.hint().contains("Start it on that machine"));
    }

    #[test]
    fn a_network_that_went_away_is_transient() {
        for line in [
            "ssh: connect to host build-box port 22: Operation timed out",
            "client_loop: send disconnect: Broken pipe",
            "Timeout, server build-box not responding.",
            "ssh: connect to host build-box port 22: No route to host",
        ] {
            let fault = classify(line).expect(line);
            assert_eq!(fault, Fault::NetworkTransient, "{line}");
            // The laptop-lid case. Giving up here is how an app becomes something you
            // restart every morning.
            assert!(!fault.permanent(), "{line}");
        }
    }

    #[test]
    fn an_unremarkable_line_is_not_a_fault() {
        for line in [
            "debug1: Reading configuration data /Users/dave/.ssh/config",
            "Authenticated to build-box ([10.0.0.4]:22) using \"publickey\".",
            "debug1: Local connections to 127.0.0.1:8765 forwarded to remote address 127.0.0.1:8765",
            "",
        ] {
            assert_eq!(classify(line), None, "{line}");
        }
    }

    #[test]
    fn every_fault_says_what_to_do_about_it() {
        for fault in [
            Fault::AuthRequired,
            Fault::HostKey,
            Fault::Unresolved,
            Fault::LocalPortBusy,
            Fault::RemoteForwardRefused,
            Fault::NetworkTransient,
        ] {
            // A classification nobody can act on is a classification that did not need
            // making. Every one of these ends up in front of a person.
            assert!(fault.hint().len() > 40, "{fault:?} has no usable hint");
        }
    }

    // --- the near end -------------------------------------------------------------------

    #[test]
    fn a_free_port_passes_preflight_and_a_taken_one_explains_itself() {
        let held = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)).unwrap();
        let taken = held.local_addr().unwrap().port();
        let complaint = preflight(taken).expect_err("the port is held by this test");
        assert!(complaint.contains(&taken.to_string()), "{complaint}");
        // The message has to name a culprit a person can go and look for.
        assert!(complaint.contains("already in use"), "{complaint}");

        drop(held);
        assert_eq!(preflight(taken), Ok(()));
    }

    #[tokio::test]
    async fn nothing_listening_is_told_apart_from_a_far_side_that_refuses() {
        // A port with nothing on it: ssh has not bound yet.
        let free = {
            let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)).unwrap();
            listener.local_addr().unwrap().port()
        };
        assert_eq!(probe(free, Duration::from_millis(500)).await, Probe::NoListener);

        // A port that accepts and immediately drops, which is byte-for-byte what `ssh -L`
        // does when the far side refuses the channel. A bare TCP connect cannot tell this
        // from a working tunnel, which is the whole reason this function exists.
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                drop(socket);
            }
        });
        assert_eq!(
            probe(port, Duration::from_millis(500)).await,
            Probe::FarSideRefused
        );
    }

    #[tokio::test]
    async fn something_that_answers_like_a_gateway_is_alive() {
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut scratch = [0_u8; 256];
                let _ = socket.read(&mut scratch).await;
                // Even a 404 proves a gateway: what is being asked is "does an HTTP server
                // answer on the far end of this forward", not "is this page there".
                let _ = socket.write_all(b"HTTP/1.1 404 Not Found\r\n\r\n").await;
            }
        });
        assert_eq!(probe(port, Duration::from_millis(500)).await, Probe::Alive);
    }

    #[test]
    fn the_ssh_we_run_is_the_system_one() {
        let ssh = program();
        // On this machine and every CI runner it is /usr/bin/ssh. The fallback is a bare
        // name, which is only reached on a box that has none -- where the failure to spawn
        // is the honest outcome.
        assert!(ssh.ends_with("ssh") || ssh.ends_with("ssh.exe"), "{ssh:?}");
    }
}
