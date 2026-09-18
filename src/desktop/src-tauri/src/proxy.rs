//! The window's only way out: two sockets this process opens on its behalf.
//!
//! ## Why the host dials and the page does not
//!
//! A browser's `WebSocket` constructor takes a URL and a subprotocol list. There is no
//! header parameter and no plan for one, so a page cannot send `Authorization: Bearer` --
//! which is why the browser build of the admin UI puts the access key in a query string,
//! and why `python-mcp-gateway-isb` sat deferred rather than picking between a cookie, a
//! subprotocol smuggle and a one-shot ticket.
//!
//! Here the question does not arise. This process holds the key, opens the socket with a
//! real header exactly as `bridge.py` does, and hands the window frames. The page never
//! sees a secret, never dials anything, and its content security policy can say so: the
//! Tauri window's `connect-src` names `ipc:` and nothing else.
//!
//! Two consequences worth writing down, because both mean *not* changing Python:
//!
//! * **`Origin` needs no new rule.** `tokio-tungstenite` sends no `Origin` header, and
//!   `transport_ws.origin_permitted(None, ...)` already returns true -- that is the branch
//!   for the bridge, for Claude Desktop, and for every non-browser client. A Tauri page
//!   dialling out directly would have needed `tauri://localhost` allowlisted; this does not.
//!   Pinned by `tests/test_desktop_contract.py::test_a_bearer_client_with_no_origin_is_accepted`.
//! * **No new key carrier.** `_offered_keys` already accepts `Authorization: Bearer`.
//!
//! ## What this layer is not allowed to understand
//!
//! Frames cross as opaque strings. Nothing here parses JSON-RPC and nothing here knows what
//! MCP is. The admin UI's middle column is a test bench whose entire value is that what you
//! exercise in it is byte-identical to what the model gets; a host that reshaped frames on
//! the way past would quietly make that false.

use std::collections::HashMap;

use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;

/// The only paths the window may ask for.
///
/// The window runs our code and nothing else, so this cannot be reached by an attacker who
/// is not already inside. It is here because the design is "the page cannot reach the
/// network", and a page that could name an arbitrary path would make that sentence false.
/// `/ui` is deliberately absent: the window loads its assets from the bundle.
pub const ALLOWED_PATHS: &[&str] = &["/mcp", "/admin"];

/// What the window receives. One per inbound frame, plus an open and a close.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Frame {
    /// The handshake completed. `rpc.js` sends `initialize` the moment it sees this.
    Open,
    /// One text frame, verbatim.
    Frame { text: String },
    /// The socket is gone. `reason` reaches the pill's tooltip in `app.js`.
    ///
    /// Unlike a browser, this host knows *why*: `tokio-tungstenite` surfaces the HTTP
    /// status, so "401" and "connection refused" are finally distinguishable to the page.
    Close { reason: String },
}

/// One live socket, from the registry's point of view.
pub struct Link {
    /// Frames on their way out. Unbounded because the alternative -- blocking a Tauri
    /// command on a full channel -- would stall the webview's IPC thread.
    pub outbound: mpsc::UnboundedSender<String>,
}

#[derive(Default)]
pub struct Links(HashMap<String, Link>);

impl Links {
    pub fn insert(&mut self, id: String, link: Link) {
        self.0.insert(id, link);
    }

    pub fn remove(&mut self, id: &str) -> Option<Link> {
        self.0.remove(id)
    }

    pub fn send(&self, id: &str, text: String) -> bool {
        match self.0.get(id) {
            Some(link) => link.outbound.send(text).is_ok(),
            None => false,
        }
    }

    /// Drop every socket. Used when the gateway restarts: the port it was on is gone, and a
    /// page still holding a link to it would wait forever instead of reconnecting.
    pub fn drain(&mut self) -> Vec<String> {
        self.0.drain().map(|(id, _)| id).collect()
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// Whether the window may open this path.
pub fn permitted(path: &str) -> bool {
    ALLOWED_PATHS.contains(&path)
}

/// The URL for a permitted path on the port the gateway reported.
pub fn endpoint(port: u16, path: &str) -> String {
    format!("ws://127.0.0.1:{port}{path}")
}

/// Open one socket and pump it until either side stops.
///
/// `on_frame` is called for the open, every inbound frame, and exactly one close. The
/// returned sender is how the window writes; dropping it closes the socket.
/// The request one socket is opened with, and the one decision in it.
///
/// `authorization` is an `Option` rather than a string that may be empty, because the two
/// cases are different in kind and the difference is the remote design. Local mode dials a
/// daemon this app started with a key it minted, and sends it. Remote mode dials a forwarded
/// port whose far end binds loopback with **no key at all** -- SSH is the authentication --
/// and so must send no header rather than an empty one, which the daemon would read as a key
/// that matches nothing. Separated out from `open` so that decision is testable without a
/// listener on the other end.
pub fn request(
    port: u16,
    path: &str,
    authorization: Option<String>,
) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request, String> {
    if !permitted(path) {
        return Err(format!("{path} is not one of {ALLOWED_PATHS:?}"));
    }
    let mut request = endpoint(port, path)
        .into_client_request()
        .map_err(|err| format!("could not build the request: {err}"))?;
    if let Some(authorization) = authorization {
        request.headers_mut().insert(
            "Authorization",
            authorization.parse().map_err(|_| "bad header")?,
        );
    }
    Ok(request)
}

pub async fn open<F>(
    port: u16,
    path: &str,
    authorization: Option<String>,
    mut on_frame: F,
) -> Result<mpsc::UnboundedSender<String>, String>
where
    F: FnMut(Frame) + Send + 'static,
{
    let request = request(port, path, authorization)?;

    let (socket, _response) = tokio_tungstenite::connect_async(request)
        .await
        // The whole error, because this is the one place that knows whether the gateway
        // said 401 or refused the connection outright, and the page is about to show it.
        .map_err(|err| err.to_string())?;

    let (mut write, mut read) = socket.split();
    let (outbound, mut rx) = mpsc::unbounded_channel::<String>();

    on_frame(Frame::Open);

    tauri::async_runtime::spawn(async move {
        let reason = loop {
            tokio::select! {
                // Outbound: the window wrote a frame, or dropped its sender.
                outgoing = rx.recv() => match outgoing {
                    Some(text) => {
                        if let Err(err) = write.send(Message::Text(text)).await {
                            break err.to_string();
                        }
                    }
                    None => break "closed by the window".to_string(),
                },
                // Inbound: the gateway said something.
                incoming = read.next() => match incoming {
                    Some(Ok(Message::Text(text))) => on_frame(Frame::Frame { text }),
                    // The gateway speaks JSON-RPC in text frames only. A binary frame is
                    // not something to guess at, so it is dropped rather than decoded.
                    Some(Ok(Message::Close(_))) | None => break "connection closed".to_string(),
                    Some(Ok(_)) => {}
                    Some(Err(err)) => break err.to_string(),
                },
            }
        };
        on_frame(Frame::Close { reason });
    });

    Ok(outbound)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_two_json_rpc_paths_are_reachable_from_the_window() {
        assert!(permitted("/mcp"));
        assert!(permitted("/admin"));
    }

    #[test]
    fn nothing_else_is() {
        for path in [
            "/",
            "/ui",
            "/ui/",
            // The daemon resolves `?key=` itself, so a path carrying one is an attempt to
            // use a carrier this host exists to retire.
            "/admin?key=stolen",
            // Traversal, in the three spellings that usually get through a naive check.
            "/mcp/../admin",
            "//mcp",
            "/MCP",
            "",
        ] {
            assert!(!permitted(path), "{path} should not be reachable");
        }
    }

    #[test]
    fn the_window_never_names_a_host_or_a_port() {
        // Both come from the supervisor's Listening state. The page passes a path and
        // nothing else, which is what keeps "the window cannot reach the network" true.
        //
        // The Connection screen now shows a machine name, and that does not weaken this:
        // the invariant is that the page cannot *choose* the socket's address. What it shows
        // is a destination the person typed and the host echoed back, and every socket still
        // goes to a loopback port this process picked -- in remote mode, the near end of a
        // forward the host opened.
        assert_eq!(endpoint(49613, "/admin"), "ws://127.0.0.1:49613/admin");
        assert!(endpoint(1, "/mcp").starts_with("ws://127.0.0.1:"));
    }

    #[test]
    fn a_local_socket_carries_the_key_and_a_remote_one_carries_no_header_at_all() {
        // The two modes, in the one line of code that tells them apart. Local dials a daemon
        // this app started with a key it minted; remote dials the near end of an SSH forward
        // whose far side binds loopback with no key, because SSH is the authentication.
        let local = request(8765, "/mcp", Some("Bearer s3cret".into())).expect("a request");
        assert_eq!(
            local.headers().get("Authorization").unwrap(),
            "Bearer s3cret"
        );

        // Absent, not empty. An empty header is a key that matches nothing, which a daemon
        // that *does* have a key would answer with a 401 -- a failure that would read as
        // "the tunnel is broken" rather than "you configured a key over there".
        let remote = request(8765, "/mcp", None).expect("a request");
        assert!(remote.headers().get("Authorization").is_none());
    }

    #[test]
    fn the_path_allowlist_is_checked_before_a_request_exists() {
        // Before the header decision and before anything is dialled, in both modes.
        assert!(request(8765, "/ui/", None).is_err());
        assert!(request(8765, "/ui/", Some("Bearer s3cret".into())).is_err());
    }

    #[test]
    fn a_frame_serialises_the_way_tauri_transport_js_reads_it() {
        let open = serde_json::to_string(&Frame::Open).unwrap();
        assert_eq!(open, r#"{"kind":"open"}"#);

        let frame = serde_json::to_string(&Frame::Frame {
            text: "{\"id\":1}".into(),
        })
        .unwrap();
        assert_eq!(frame, r#"{"kind":"frame","text":"{\"id\":1}"}"#);

        let close = serde_json::to_string(&Frame::Close {
            reason: "401".into(),
        })
        .unwrap();
        assert_eq!(close, r#"{"kind":"close","reason":"401"}"#);
    }

    #[test]
    fn a_write_to_a_socket_that_is_already_gone_is_false_rather_than_a_panic() {
        let links = Links::default();
        // The close frame and a last write race in the ordinary run of things. The command
        // turns this into a successful no-op rather than an error, so the page does not
        // report a second failure for one closure.
        assert!(!links.send("no-such-id", "{}".into()));
    }

    #[test]
    fn draining_the_registry_names_everything_it_dropped() {
        let mut links = Links::default();
        for id in ["a", "b"] {
            let (tx, _rx) = mpsc::unbounded_channel();
            links.insert(id.to_string(), Link { outbound: tx });
        }
        assert_eq!(links.len(), 2);

        let mut dropped = links.drain();
        dropped.sort();
        assert_eq!(dropped, vec!["a", "b"]);
        assert!(links.is_empty(), "a restart must leave no link behind");
    }

    #[test]
    fn a_removed_link_is_gone_and_removing_it_twice_is_harmless() {
        let mut links = Links::default();
        let (tx, _rx) = mpsc::unbounded_channel();
        links.insert("one".into(), Link { outbound: tx });

        assert!(links.remove("one").is_some());
        assert!(links.remove("one").is_none());
    }
}
