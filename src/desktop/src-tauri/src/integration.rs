//! The whole runtime path, minus the window: spawn a real gateway and talk to it.
//!
//! The unit tests beside each module cover the parts in isolation -- the parser, the
//! backoff, the seeding, the path allowlist. This drives them together against the actual
//! bundled interpreter, which is the only way to find out whether the pieces agree:
//!
//! * the interpreter is where `bundle_python.py` said it would be, and runs;
//! * `--port 0` plus the stderr scrape really does yield a port that answers;
//! * the minted key reaches the daemon through the environment and is *required*;
//! * a `tokio-tungstenite` client with a Bearer header and no `Origin` gets in, which is
//!   the assumption the whole design rests on;
//! * frames cross intact in both directions;
//! * teardown leaves nothing behind.
//!
//! Requires `make tauri-python`. Skipped -- not failed -- without it, so `cargo test` on a
//! fresh checkout stays green, the same discipline `tests/test_webui_js.py` uses for node.
//!
//! In the crate rather than in `tests/`, because `AccessKey::as_env` and `header` are
//! `pub(crate)` deliberately: the key reaching exactly two call sites is a property worth
//! more than a tidier directory, and an external test crate would need it widened.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::appdata;
use crate::key::AccessKey;
use crate::proxy::{self, Frame};
use crate::supervisor::{self, Layout};

/// The bundle, and the seed files, as a checkout has them.
fn layout(scratch: &str) -> Option<(Layout, PathBuf)> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let python_root = root.join("resources").join("python");
    let data_dir = std::env::temp_dir().join(format!("mcp-gateway-it-{scratch}"));
    let _ = std::fs::remove_dir_all(&data_dir);
    let layout = Layout {
        python_root,
        data_dir,
    };
    // Asked through `Layout`, not by joining `bin/python3` here: the interpreter is at the
    // root of the tree on Windows, and a second spelling of that path is a second thing to
    // get wrong -- one that would silently skip the only tests that run the real thing.
    if !layout.interpreter().exists() {
        eprintln!("skipping: no bundled interpreter -- run `make tauri-python`");
        return None;
    }
    Some((layout, root.join("seed")))
}

/// Start the gateway and wait for the port file it writes.
///
/// The pump runs as its own task for the life of the test, which is how the real supervisor
/// uses it: stderr keeps arriving while the socket is being used. It no longer carries the
/// port -- that comes from `wait_for_port` -- but it is still what a failure is explained
/// with, so it is collected and printed when the wait times out.
async fn start(layout: &Layout, key: &str) -> (tokio::process::Child, u16, Daemon) {
    let port_file = layout.portfile();
    let _ = std::fs::remove_file(&port_file);

    let mut child = supervisor::spawn(layout, key, &std::env::var("PATH").unwrap(), 0)
        .expect("the bundled interpreter should start");

    let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let stderr = child.stderr.take().expect("stderr is piped");
    let sink = seen.clone();
    tokio::spawn(supervisor::pump_stderr(stderr, move |line| {
        sink.lock().unwrap().push(line)
    }));

    match supervisor::wait_for_port(&port_file, supervisor::STARTUP_TIMEOUT).await {
        Some(port) => (child, port, Daemon { seen }),
        None => panic!(
            "no port file at {}; the daemon said:\n{}",
            port_file.display(),
            seen.lock().unwrap().join("\n")
        ),
    }
}

/// What the daemon has said so far, for explaining a failure that is not a timeout.
///
/// This exists because of a failure that cost a CI round trip to read. The two tests that
/// dial the socket failed with a bare `os error 10061` -- the connect was refused -- and
/// the stderr that would have said whether the daemon was even alive was being collected
/// and then dropped on the floor, because only `wait_for_port` printed it. The cause was a
/// sibling test's teardown killing this daemon through a shared Job Object (see
/// `supervisor::job`), which the log below states plainly and the bare `10061` did not.
struct Daemon {
    seen: Arc<Mutex<Vec<String>>>,
}

impl Daemon {
    /// `context`, plus whether the process is still running and everything it has said.
    ///
    /// `try_wait` is the question worth asking first: a refused connection means one of
    /// two very different things, and "the daemon exited with status 1" and "the daemon is
    /// running and did not accept" send you to opposite ends of the codebase.
    fn explain(&self, child: &mut tokio::process::Child, context: &str) -> String {
        let alive = match child.try_wait() {
            Ok(None) => "still running".to_string(),
            Ok(Some(status)) => format!("ALREADY EXITED ({status})"),
            Err(err) => format!("could not be waited on: {err}"),
        };
        format!(
            "{context}\n  daemon: {alive}\n  stderr:\n{}",
            self.seen.lock().unwrap().join("\n")
        )
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_bundled_gateway_starts_and_answers_the_shell() {
    let Some((layout, seed)) = layout("answers") else {
        return;
    };
    appdata::ensure(&layout.data_dir, &seed).expect("first-run seeding");

    let key = AccessKey::mint();
    let (mut child, port, daemon) = start(&layout, key.as_env()).await;
    assert_ne!(
        port, 8765,
        "`--port 0` must not land on the daemon's default"
    );

    // Exactly what `gw_open` does, including the header and the absence of an `Origin`.
    let frames: Arc<Mutex<Vec<Frame>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = frames.clone();
    let outbound = match proxy::open(port, "/admin", Some(key.header()), move |frame| {
        sink.lock().unwrap().push(frame)
    })
    .await
    {
        Ok(socket) => socket,
        Err(err) => panic!(
            "{}",
            daemon.explain(
                &mut child,
                &format!("a Bearer client with no Origin should be admitted, got: {err}")
            )
        ),
    };

    outbound
        .send(r#"{"jsonrpc":"2.0","id":1,"method":"admin.status"}"#.to_string())
        .unwrap();

    let reply = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            if let Some(Frame::Frame { text }) = frames
                .lock()
                .unwrap()
                .iter()
                .find(|f| matches!(f, Frame::Frame { .. }))
                .cloned()
            {
                return text;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .expect("the gateway should have answered admin.status");

    // Relayed verbatim: the proxy parses nothing, so what arrives is the daemon's own JSON.
    let value: serde_json::Value = serde_json::from_str(&reply).unwrap();
    assert_eq!(value["id"], 1);
    assert!(value["result"]["version"].is_string(), "{value}");
    assert!(value["result"]["bind"]
        .as_str()
        .unwrap()
        .ends_with(&port.to_string()));

    supervisor::terminate(&mut child).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn the_key_is_required_and_the_window_cannot_name_another_path() {
    let Some((layout, seed)) = layout("auth") else {
        return;
    };
    appdata::ensure(&layout.data_dir, &seed).expect("first-run seeding");

    let key = AccessKey::mint();
    let (mut child, port, daemon) = start(&layout, key.as_env()).await;

    // Without the header: the daemon refuses. If this ever passes, the app is handing the
    // machine's whole credential store to anything that can reach loopback.
    let refused = proxy::open(port, "/admin", Some("Bearer not-the-key".into()), |_| {}).await;
    assert!(refused.is_err(), "a wrong key must not get in");
    let reason = refused.unwrap_err();
    // A refusal is only evidence if it came from the daemon. A dead daemon refuses every
    // connection, including this one, and would let a broken key check pass as a pass.
    assert!(
        reason.contains("401"),
        "{}",
        daemon.explain(
            &mut child,
            &format!("the refusal should be a 401 from the daemon, got: {reason}")
        )
    );

    // The path allowlist is enforced before anything is dialled.
    let blocked = proxy::open(port, "/ui/", Some(key.header()), |_| {}).await;
    assert!(blocked.is_err());

    supervisor::terminate(&mut child).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn stopping_the_app_stops_the_gateway() {
    let Some((layout, seed)) = layout("teardown") else {
        return;
    };
    appdata::ensure(&layout.data_dir, &seed).expect("first-run seeding");

    let key = AccessKey::mint();
    let (mut child, port, _daemon) = start(&layout, key.as_env()).await;
    let pid = child.id().expect("a running child has a pid");

    supervisor::terminate(&mut child).await;

    // The port is free again, which is the observable form of "nothing is left running".
    // A gateway that outlived its app would still be holding every credential in
    // `gateway.env`, which is why this is a test and not a hope.
    let rebind = std::net::TcpListener::bind(("127.0.0.1", port));
    assert!(
        rebind.is_ok(),
        "port {port} is still held; pid {pid} may have survived"
    );
}

/// What remote mode actually reaches, without needing a second machine.
///
/// The far side of an SSH forward is a daemon somebody else started, bound to loopback with
/// **no access key at all** -- SSH is the authentication -- and what arrives through the
/// forward is a client with no `Origin` and no `Authorization`. Both of those are properties
/// of the Python, tested there; what is tested here is that this app's own client really
/// does get in without a header, and that `tunnel::probe` can tell a gateway on the far end
/// from a forward with nothing behind it.
///
/// The keyless daemon is the whole point, so it is spawned the way remote mode's really is:
/// with an empty `MCP_GATEWAY_WS_KEY`, which `access_key_from_env` reads as unset.
#[tokio::test(flavor = "multi_thread")]
async fn a_keyless_gateway_is_what_remote_mode_reaches() {
    let Some((layout, seed)) = layout("keyless") else {
        return;
    };
    appdata::ensure(&layout.data_dir, &seed).expect("first-run seeding");

    let (mut child, port, daemon) = start(&layout, "").await;

    // No header, and it gets in. If this ever fails, remote mode has no way to connect at
    // all -- and if it were to start *requiring* one, the failure would look like a broken
    // tunnel rather than like a policy change.
    let frames: Arc<Mutex<Vec<Frame>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = frames.clone();
    let opened = proxy::open(port, "/admin", None, move |frame| {
        sink.lock().unwrap().push(frame)
    })
    .await;
    assert!(
        opened.is_ok(),
        "{}",
        daemon.explain(
            &mut child,
            &format!(
                "a keyless daemon should accept a header-less client, got: {:?}",
                opened.as_ref().err()
            )
        )
    );

    // The probe, against the three things it has to tell apart -- the live one first,
    // because it is the only one that needs a real gateway.
    assert_eq!(
        crate::tunnel::probe(port, Duration::from_secs(2)).await,
        crate::tunnel::Probe::Alive,
        "{}",
        daemon.explain(&mut child, "the daemon should answer the probe like a gateway")
    );

    supervisor::terminate(&mut child).await;

    // And once it is gone, the same port is nothing at all. This is the shape of the
    // question the tunnel supervisor asks every quarter second.
    assert_eq!(
        crate::tunnel::probe(port, Duration::from_millis(500)).await,
        crate::tunnel::Probe::NoListener
    );
}

/// A tunnel is a child like any other: it dies with the app.
///
/// `ssh` is not available to a test as a *forwarding* process without a second machine, but
/// the property that matters here is not ssh's -- it is `spawn_hardened`'s, which is what
/// both supervisors use and what the three layers of teardown rest on. So this drives it
/// with a stand-in child that does the one thing an unsupervised tunnel would do: outlive us.
#[tokio::test(flavor = "multi_thread")]
async fn a_hardened_child_that_is_not_the_daemon_still_cannot_outlive_us() {
    let mut command = tokio::process::Command::new(if cfg!(windows) { "cmd" } else { "sh" });
    if cfg!(windows) {
        command.args(["/C", "ping -n 30 127.0.0.1"]);
    } else {
        command.args(["-c", "sleep 30"]);
    }
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);

    let mut child = supervisor::spawn_hardened(&mut command).expect("the stand-in starts");
    let pid = child.id().expect("a running child has a pid");
    supervisor::terminate(&mut child).await;

    // Reaped, rather than left for the OS. A tunnel that survived its window would keep a
    // forwarded port open with nobody watching it.
    assert!(
        child.try_wait().expect("waitable").is_some(),
        "pid {pid} should be gone"
    );
}
