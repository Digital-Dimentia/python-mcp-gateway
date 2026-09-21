//! What the window is told about the gateway: the phases, and the snapshot it renders.
//!
//! Both supervisors — the local child in `supervisor.rs`, the SSH forward in `tunnel.rs` —
//! report through this one vocabulary, and the window has one place to read.
//!
//! ## Why the five old words survive
//!
//! `Status.state` is still exactly `idle | starting | listening | restarting | failed`,
//! because `app.js` ranks those five (`GATE_RANK`) and refuses to let a lower-ranked writer
//! paint over a higher-ranked one. That ranking is what stopped the window reporting "this
//! gateway may require an access key" while a cold interpreter was simply still importing,
//! and every new phase has to fit inside it rather than beside it.
//!
//! So the phases are richer and the `state` is not. `OpeningTunnel` is a kind of `starting`:
//! nothing is wrong, the thing is not ready. `FarSideSilent` — the forward is open and no
//! gateway is running over there — is *also* `starting`, which is the interesting one: it is
//! not a failure of this app, nothing here can fix it, and ranking it as `failed` would let
//! it outrank a real failure that arrived a moment later. What tells them apart in the
//! window is `detail`, which is a true sentence in every phase rather than only in the bad
//! ones.
//!
//! ## Why `detail` and `connection` are here and not in the daemon
//!
//! A remote daemon answers `admin.status` with `bind: 127.0.0.1:8765` — exactly what a local
//! one says, because it is exactly what it did. The one field that looks like it says which
//! machine you are driving is the one field that cannot. So everything about *where* comes
//! from this process, which is the only one that knows.

use serde::Serialize;

use crate::settings::{Connection, Mode};

/// Where the gateway has got to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phase {
    /// Nothing is running and nothing is being attempted.
    Idle,
    /// Nothing is running because somebody asked for nothing to be running.
    ///
    /// Distinct from `Idle` for one reason, and it is the window's: `Idle` is what the
    /// window sees for the moment between opening and the first spawn, so the page paints a
    /// splash over itself and waits. A deliberate stop lasts until somebody acts, and a
    /// splash that never resolves is an app with no way back into it. Same coarse `state`,
    /// because nothing else in the page needs to know the difference.
    Stopped,
    /// The local child has been spawned and has not written its port file yet.
    StartingDaemon,
    /// `ssh` has been spawned and has not bound the near end yet.
    OpeningTunnel,
    /// The forward accepts connections and nothing has answered like a gateway through it.
    ///
    /// Kept apart from `FarSideSilent` because they want different words: this one is still
    /// coming up, and that one is a thing the person has to go and fix somewhere else.
    TunnelUp { port: u16 },
    /// The forward works and there is no gateway listening on the far side of it.
    FarSideSilent { port: u16 },
    /// Ready. Sockets may be opened on this port.
    Listening { port: u16 },
    /// It went away and is being started again.
    Restarting { attempt: u32, why: Option<String> },
    /// Gave up. `reason` is the part worth showing.
    Failed { reason: String },
}

impl Phase {
    /// The port to dial, when there is one.
    pub fn port(&self) -> Option<u16> {
        match *self {
            Phase::Listening { port } => Some(port),
            _ => None,
        }
    }

    /// The five-word vocabulary `app.js` ranks. See the module docs.
    pub fn state(&self) -> &'static str {
        match self {
            Phase::Idle | Phase::Stopped => "idle",
            Phase::StartingDaemon | Phase::OpeningTunnel | Phase::TunnelUp { .. } => "starting",
            // Not a failure: the tunnel is fine and the daemon over there is not running.
            Phase::FarSideSilent { .. } => "starting",
            Phase::Listening { .. } => "listening",
            Phase::Restarting { .. } => "restarting",
            Phase::Failed { .. } => "failed",
        }
    }

    /// The finer name, for the Connection screen.
    pub fn name(&self) -> &'static str {
        match self {
            Phase::Idle => "idle",
            Phase::Stopped => "stopped",
            Phase::StartingDaemon => "starting-daemon",
            Phase::OpeningTunnel => "opening-tunnel",
            Phase::TunnelUp { .. } => "tunnel-up",
            Phase::FarSideSilent { .. } => "far-side-silent",
            Phase::Listening { .. } => "listening",
            Phase::Restarting { .. } => "restarting",
            Phase::Failed { .. } => "failed",
        }
    }

    /// One true sentence, in every phase rather than only the bad ones.
    ///
    /// The gate shows this, so a window that is merely waiting still says what it is waiting
    /// for — which is the difference between "Starting…" forever and "the tunnel is open,
    /// and nothing is listening over there".
    pub fn detail(&self, connection: &Connection) -> Option<String> {
        let where_ = connection.label().unwrap_or("this machine");
        Some(match self {
            Phase::Idle => return None,
            //: Says what it is *and* what to do, because this is the one phase the window
            //: cannot leave on its own -- see the variant's own note.
            Phase::Stopped => format!("Disconnected from {where_}. Press Connect to start again.",),
            Phase::StartingDaemon => "Starting the gateway…".into(),
            Phase::OpeningTunnel => format!("Opening an SSH tunnel to {where_}…"),
            Phase::TunnelUp { .. } => {
                format!("The tunnel to {where_} is open; waiting for the gateway there…")
            }
            Phase::FarSideSilent { .. } => format!(
                "The tunnel is open, but nothing is listening on 127.0.0.1:{} on {where_}. \
                 Start the gateway there.",
                connection.remote_port
            ),
            Phase::Listening { .. } => return None,
            Phase::Restarting { why, .. } => why.clone().unwrap_or_else(|| {
                if connection.mode == Mode::Remote {
                    format!("The tunnel to {where_} dropped; reopening…")
                } else {
                    "The gateway stopped; restarting…".into()
                }
            }),
            Phase::Failed { reason } => reason.clone(),
        })
    }
}

/// Where the gateway runs, as the window renders it.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Where {
    /// `local` or `remote`.
    pub mode: &'static str,
    /// The machine, when it is not this one. `None` is what every local-mode call site below
    /// checks for, which is what makes local mode's window unchanged.
    pub label: Option<String>,
    pub local_port: u16,
    pub remote_port: u16,
    /// The line to paste into a client on this machine, when there is a port to name.
    ///
    /// Built here rather than in the page: it carries the path of the bridge inside this
    /// bundle, and a line assembled in JavaScript would be a line that only works for
    /// whoever put the app in `/Applications`.
    pub connect_command: Option<String>,
}

/// The snapshot the window renders from.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    /// `idle` | `starting` | `listening` | `restarting` | `failed`. See the module docs.
    pub state: &'static str,
    /// The finer phase, for the Connection screen.
    pub phase: &'static str,
    pub attempt: u32,
    /// Why it is not running, when it is not.
    pub reason: Option<String>,
    /// One sentence about the current phase, good or bad.
    pub detail: Option<String>,
    /// The last few stderr lines — the daemon's, or ssh's.
    pub log: Vec<String>,
    pub connection: Where,
}

/// Which `Authorization` header a socket in this mode carries, if any.
///
/// Local mode dials a daemon this app started with a key it minted. Remote mode dials the
/// near end of a forward whose far side binds loopback with no key at all — SSH is the
/// authentication — and so must send no header rather than an empty one, which a daemon that
/// *did* have a key would answer with a 401 that reads like a broken tunnel.
pub fn authorization(mode: Mode, key: &crate::key::AccessKey) -> Option<String> {
    match mode {
        Mode::Local => Some(key.header()),
        Mode::Remote => None,
    }
}

/// The `claude mcp add` line for a gateway reachable on `port` of this machine.
pub fn connect_command(bridge: &std::path::Path, port: u16) -> String {
    format!(
        "claude mcp add gateway -- {} --url ws://127.0.0.1:{port}/mcp",
        bridge.display()
    )
}

/// Assemble the snapshot. Pure, so the mapping above is testable without an app.
pub fn status_of(
    phase: &Phase,
    connection: &Connection,
    log: Vec<String>,
    bridge: &std::path::Path,
) -> Status {
    let (attempt, reason) = match phase {
        Phase::Restarting { attempt, .. } => (*attempt, None),
        Phase::Failed { reason } => (0, Some(reason.clone())),
        _ => (0, None),
    };

    // A port worth telling a client about is one that will still be there tomorrow: the
    // forward's near end in remote mode, and in local mode only a port somebody pinned.
    // An ephemeral one is true right now and wrong after the next restart, so it is not
    // offered at all.
    let client_port = match connection.mode {
        Mode::Remote => Some(connection.local_port),
        Mode::Local if connection.local_port != 0 => Some(connection.local_port),
        Mode::Local => None,
    };

    Status {
        state: phase.state(),
        phase: phase.name(),
        attempt,
        reason,
        detail: phase.detail(connection),
        log,
        connection: Where {
            mode: match connection.mode {
                Mode::Local => "local",
                Mode::Remote => "remote",
            },
            label: connection.label().map(str::to_string),
            local_port: connection.local_port,
            remote_port: connection.remote_port,
            connect_command: client_port.map(|port| connect_command(bridge, port)),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn bridge() -> &'static Path {
        Path::new("/Applications/MCP-Gateway.app/Contents/Resources/python/bin/mcp-gateway-connect")
    }

    fn every_phase() -> Vec<Phase> {
        vec![
            Phase::Idle,
            Phase::StartingDaemon,
            Phase::OpeningTunnel,
            Phase::TunnelUp { port: 8765 },
            Phase::FarSideSilent { port: 8765 },
            Phase::Listening { port: 8765 },
            Phase::Restarting {
                attempt: 2,
                why: None,
            },
            Phase::Failed {
                reason: "nope".into(),
            },
        ]
    }

    #[test]
    fn every_phase_maps_to_a_state_the_gate_can_rank() {
        // `app.js` ranks exactly these five and refuses to let a lower one paint over a
        // higher one. A sixth word would not be ranked, and would silently win or lose.
        let known = ["idle", "starting", "listening", "restarting", "failed"];
        for phase in every_phase() {
            assert!(
                known.contains(&phase.state()),
                "{phase:?} -> {}",
                phase.state()
            );
        }
    }

    #[test]
    fn the_far_side_being_silent_is_not_a_failure() {
        // The tunnel is perfect and the daemon over there is not running. Ranking this as
        // `failed` would let it paint over a real failure arriving a moment later -- and
        // would tell the person this app is broken when nothing here is.
        let phase = Phase::FarSideSilent { port: 8765 };
        assert_eq!(phase.state(), "starting");
        assert_eq!(phase.name(), "far-side-silent");

        let detail = phase
            .detail(&Connection::remote("build-box"))
            .expect("a sentence");
        assert!(detail.contains("build-box"), "{detail}");
        assert!(detail.contains("8765"), "{detail}");
        assert!(detail.contains("Start the gateway there"), "{detail}");
    }

    #[test]
    fn a_tunnel_that_is_opening_says_where_to() {
        let detail = Phase::OpeningTunnel
            .detail(&Connection::remote("dave@build-box"))
            .expect("a sentence");
        assert!(detail.contains("dave@build-box"), "{detail}");
    }

    #[test]
    fn local_mode_never_names_a_machine() {
        let local = Connection::default();
        for phase in every_phase() {
            let status = status_of(&phase, &local, vec![], bridge());
            assert_eq!(status.connection.mode, "local");
            assert_eq!(status.connection.label, None);
        }
    }

    #[test]
    fn only_a_port_that_survives_a_restart_is_offered_to_a_client() {
        // Local and ephemeral: there is no line to give anybody, because the number is
        // different after the next launch and a client config is written once.
        let ephemeral = status_of(
            &Phase::Listening { port: 49613 },
            &Connection::default(),
            vec![],
            bridge(),
        );
        assert_eq!(ephemeral.connection.connect_command, None);

        // Local and pinned: the app's own daemon, at a number somebody chose.
        let mut pinned = Connection::default();
        pinned.local_port = 8765;
        let pinned = status_of(&Phase::Listening { port: 8765 }, &pinned, vec![], bridge());
        let command = pinned.connection.connect_command.expect("a line to paste");
        assert!(command.contains("ws://127.0.0.1:8765/mcp"), "{command}");
        assert!(command.contains("mcp-gateway-connect"), "{command}");

        // Remote: the near end of the forward, which is where a client on this machine goes.
        let mut remote = Connection::remote("build-box");
        remote.local_port = 9000;
        remote.remote_port = 8765;
        let remote = status_of(&Phase::Listening { port: 9000 }, &remote, vec![], bridge());
        assert!(remote
            .connection
            .connect_command
            .unwrap()
            .contains("ws://127.0.0.1:9000/mcp"));
    }

    #[test]
    fn the_status_carries_no_secret() {
        let key = crate::key::AccessKey::mint();
        // Not that it could: nothing in `Status` is built from the key. This is the test
        // that notices if somebody ever puts it there for convenience.
        let status = status_of(
            &Phase::Listening { port: 8765 },
            &Connection::remote("build-box"),
            vec!["debug1: Authenticated to build-box".into()],
            bridge(),
        );
        let json = serde_json::to_string(&status).unwrap();
        assert!(!json.contains(key.as_env()));
        assert!(!json.to_lowercase().contains("bearer"));
    }

    #[test]
    fn remote_mode_sends_no_authorization_header() {
        let key = crate::key::AccessKey::mint();
        assert_eq!(authorization(Mode::Remote, &key), None);
        assert_eq!(authorization(Mode::Local, &key), Some(key.header()));
    }

    #[test]
    fn a_failure_is_both_the_reason_and_the_detail() {
        // The gate reads one and the Connection screen reads the other; a failure that
        // filled only one of them would be invisible in the other place.
        let status = status_of(
            &Phase::Failed {
                reason: "Permission denied (publickey).".into(),
            },
            &Connection::remote("build-box"),
            vec![],
            bridge(),
        );
        assert_eq!(status.state, "failed");
        assert_eq!(
            status.reason.as_deref(),
            Some("Permission denied (publickey).")
        );
        assert_eq!(
            status.detail.as_deref(),
            Some("Permission denied (publickey).")
        );
    }

    #[test]
    fn a_restart_carries_its_attempt_and_its_reason() {
        let status = status_of(
            &Phase::Restarting {
                attempt: 3,
                why: Some("the network went away".into()),
            },
            &Connection::remote("build-box"),
            vec![],
            bridge(),
        );
        assert_eq!(status.state, "restarting");
        assert_eq!(status.attempt, 3);
        assert_eq!(status.detail.as_deref(), Some("the network went away"));
    }
}
