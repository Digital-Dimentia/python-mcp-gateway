//! The one document this window loads that nobody here wrote: a backend's HTML panel.
//!
//! ## Why the window cannot simply frame the daemon's URL
//!
//! The page is served from `tauri://localhost` off the bundle, and its content security
//! policy names `ipc:` and nothing else — that is the whole shape of this shell, and
//! `proxy.rs` explains why: the window reaches the network through this process or not at
//! all. So `<iframe src="/panel/<token>">` resolves against `tauri://localhost` and finds
//! nothing, and pointing it at `http://127.0.0.1:<port>/panel/<token>` is refused by the
//! policy before a request exists. Both failures are the design working.
//!
//! A custom URI scheme is the door the design leaves open: this process answers it, so the
//! window is still reaching the network *through* us. `panel://localhost/<token>` on macOS
//! and Linux, `http://panel.localhost/<token>` on Windows, which is the same scheme spelt
//! the way WebView2 requires.
//!
//! ## This layer does not know what a panel policy is
//!
//! It fetches `/panel/<token>` from the daemon and **copies the response's
//! `Content-Security-Policy` header verbatim** onto what it hands the webview. It does not
//! build that policy, parse it, or hold an opinion about it — `panels.py` built it from the
//! backend's own sanitized `csp`, and a second implementation here would be a second thing
//! to keep in step and a second thing to get wrong. Same doctrine as `proxy.rs`: frames
//! cross as opaque strings, and a layer that reshaped them would quietly make the browser
//! and the shell disagree about what a panel may do.
//!
//! The header that matters is `sandbox allow-scripts`, and the consequence of forwarding it
//! is the same here as there: the document lands in an **opaque origin**, so it cannot read
//! this window's storage, reach `tauri://localhost`, or see the IPC. If this copy is ever
//! dropped, a panel becomes a script running inside the shell's own origin — which is
//! strictly worse than the browser case, because this origin can invoke commands.
//!
//! ## The token is the whole request
//!
//! A token is checked against the alphabet `secrets.token_urlsafe` produces before it is
//! written into a request line. That is not tidiness: this module composes an HTTP request
//! by hand, so a token carrying a space or a newline would be request splitting, from a
//! string that arrived over IPC. The check is an allowlist, the failure is a refusal, and
//! `a_token_that_could_split_a_request_is_refused` is the test that says so.

use std::io;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

/// The scheme this window frames a panel on. Registered in `lib.rs`.
pub const SCHEME: &str = "panel";

/// The daemon's path for a panel, which is also the shape `admin.panel.open` answers with.
pub const PANEL_PATH: &str = "/panel";

/// A panel URL is single-use and short-lived, so a fetch that has not finished in this long
/// has not failed to be quick — it has failed. Matching `TOKEN_TTL_SECONDS` on the other
/// side would be pointless: the token is already spent by the time a read blocks.
const FETCH_TIMEOUT: Duration = Duration::from_secs(10);

/// A ceiling on a panel document. Generous for a page of HTML, and the thing that stops a
/// backend from making this process hold a gigabyte because the window asked for a frame.
const MAX_BODY: usize = 8 * 1024 * 1024;

/// What a panel URL may contain, and nothing else: the alphabet of
/// `secrets.token_urlsafe`. See the module docs — this string goes into a request line.
fn is_token(token: &str) -> bool {
    !token.is_empty()
        && token.len() <= 256
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// The token in a path the page hands us, or `None` when it is not one.
///
/// Accepts `/panel/<token>` and a bare `<token>`, because the page has the first and the
/// scheme handler sees the second, and having one function say what a token is beats two
/// that agree until somebody edits one.
pub fn token_of(path: &str) -> Option<&str> {
    let trimmed = path.trim_start_matches('/');
    let token = match trimmed.strip_prefix("panel/") {
        Some(rest) => rest,
        None => trimmed,
    };
    // One segment. A query string is not part of a capability URL here, and a second
    // segment is not a token to search for -- it is a request for something else.
    if token.contains('/') || token.contains('?') || token.contains('#') {
        return None;
    }
    is_token(token).then_some(token)
}

/// The URL to put in an `<iframe src>`, for a token the daemon minted.
///
/// Platform knowledge lives here rather than in the page: WebView2 serves a custom scheme
/// as `http://<scheme>.localhost/`, and every other platform as `<scheme>://localhost/`. A
/// page that branched on `navigator` would be guessing at what its own host does.
pub fn framable(token: &str) -> String {
    #[cfg(windows)]
    {
        format!("http://{SCHEME}.localhost/{token}")
    }
    #[cfg(not(windows))]
    {
        format!("{SCHEME}://localhost/{token}")
    }
}

/// One panel, as the daemon served it.
#[derive(Debug, Clone)]
pub struct Panel {
    pub status: u16,
    pub body: Vec<u8>,
    /// Copied from the response, never composed here. Empty only if the daemon sent none,
    /// which `lib.rs` treats as a refusal rather than as "no policy needed".
    pub csp: String,
    pub content_type: String,
}

/// Fetch `/panel/<token>` from the daemon on `port`.
///
/// A hand-written HTTP/1.1 GET over loopback rather than a client crate, for the reason
/// `Cargo.toml` gives about TLS: the one address this process dials is 127.0.0.1, and the
/// request is eight lines. `Connection: close` means the body ends at EOF and there is no
/// chunked decoding to get wrong — the daemon's `panels.py` always sends `Content-Length`,
/// and reading to EOF is correct whether or not it does.
///
/// **No access key.** The token in the path is the credential; `panels.md` is where that
/// argument lives. Sending the key as well would be harmless and would also be the first
/// step towards this endpoint quietly depending on one.
pub async fn fetch(port: u16, token: &str) -> io::Result<Panel> {
    if !is_token(token) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "not a panel token",
        ));
    }
    let raw = tokio::time::timeout(FETCH_TIMEOUT, async {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).await?;
        let request = format!(
            "GET {PANEL_PATH}/{token} HTTP/1.1\r\n\
             Host: 127.0.0.1:{port}\r\n\
             Accept: text/html\r\n\
             Connection: close\r\n\r\n"
        );
        stream.write_all(request.as_bytes()).await?;
        stream.flush().await?;

        let mut raw = Vec::new();
        let mut chunk = [0u8; 8192];
        loop {
            let read = stream.read(&mut chunk).await?;
            if read == 0 {
                break;
            }
            raw.extend_from_slice(&chunk[..read]);
            if raw.len() > MAX_BODY {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "panel is too large",
                ));
            }
        }
        Ok::<Vec<u8>, io::Error>(raw)
    })
    .await
    .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "the gateway did not answer"))??;

    parse(&raw)
}

/// Split one HTTP/1.1 response into the three things this module carries.
///
/// Deliberately not a general parser: it reads the status line, the two headers that are
/// forwarded, and treats everything after the blank line as the body. Anything else the
/// daemon sends is dropped, which is the right default for a layer whose job is to carry
/// one document and not to be an HTTP proxy.
fn parse(raw: &[u8]) -> io::Result<Panel> {
    let split = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no end of headers"))?;
    let head = String::from_utf8_lossy(&raw[..split]);
    let body = raw[split + 4..].to_vec();

    let mut lines = head.lines();
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no status line"))?;

    let mut csp = String::new();
    let mut content_type = String::new();
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        match name.trim().to_ascii_lowercase().as_str() {
            "content-security-policy" => csp = value.trim().to_string(),
            "content-type" => content_type = value.trim().to_string(),
            _ => {}
        }
    }
    Ok(Panel {
        status,
        body,
        csp,
        content_type,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_is_one_segment_of_the_alphabet_python_mints() {
        assert_eq!(token_of("/panel/abc-DEF_123"), Some("abc-DEF_123"));
        assert_eq!(token_of("abc-DEF_123"), Some("abc-DEF_123"));
        assert_eq!(token_of("/panel/"), None);
        assert_eq!(token_of("/panel/a/b"), None);
        assert_eq!(token_of("/ui/index.html"), None);
    }

    #[test]
    fn a_token_that_could_split_a_request_is_refused() {
        // Each of these composes into a second request line, a second header, or a path
        // that is not the one the daemon minted. The check is an allowlist for this reason.
        for hostile in [
            "abc def",
            "abc\r\nX-Evil: 1",
            "abc\nHost: elsewhere",
            "../../ui/index.html",
            "abc%0d%0a",
            "abc?key=secret",
            "abc#frag",
            "",
        ] {
            assert_eq!(token_of(hostile), None, "{hostile:?} must not be a token");
        }
    }

    #[test]
    fn a_token_is_bounded_so_a_request_line_cannot_be_grown_without_limit() {
        let longest = "a".repeat(256);
        assert!(token_of(&longest).is_some());
        assert_eq!(token_of(&"a".repeat(257)), None);
    }

    #[test]
    fn the_framable_url_names_this_process_rather_than_the_daemon() {
        let url = framable("tok123");
        assert!(url.contains("tok123"));
        assert!(
            !url.contains("127.0.0.1"),
            "the window never names the daemon's address"
        );
        assert!(url.starts_with(SCHEME) || url.starts_with("http://panel.localhost"));
    }

    #[test]
    fn the_policy_is_read_off_the_response_rather_than_composed() {
        let raw = b"HTTP/1.1 200 OK\r\n\
                    Content-Type: text/html; charset=utf-8\r\n\
                    Content-Security-Policy: sandbox allow-scripts; default-src 'none'\r\n\
                    Content-Length: 5\r\n\r\nhello";
        let panel = parse(raw).expect("a well-formed response");
        assert_eq!(panel.status, 200);
        assert_eq!(panel.body, b"hello");
        assert_eq!(panel.csp, "sandbox allow-scripts; default-src 'none'");
        assert_eq!(panel.content_type, "text/html; charset=utf-8");
    }

    #[test]
    fn a_header_name_in_another_case_is_still_that_header() {
        let raw = b"HTTP/1.1 200 OK\r\nCONTENT-SECURITY-POLICY: sandbox\r\n\r\nx";
        assert_eq!(parse(raw).expect("parsed").csp, "sandbox");
    }

    #[test]
    fn a_404_comes_back_as_a_404_rather_than_as_an_error() {
        // A spent or unknown token is an ordinary answer, and the window shows what it
        // means. Turning it into a transport failure would lose that.
        let raw = b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNo such panel.\n";
        let panel = parse(raw).expect("parsed");
        assert_eq!(panel.status, 404);
        assert!(panel.csp.is_empty());
    }

    #[test]
    fn a_response_with_no_end_of_headers_is_an_error_not_a_guess() {
        assert!(parse(b"HTTP/1.1 200 OK\r\nContent-Type: text/html").is_err());
    }

    /// A listener that answers one request with `response` and says what it was asked.
    async fn one_shot(response: &'static [u8]) -> (u16, tokio::task::JoinHandle<String>) {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("bound");
        let port = listener.local_addr().expect("an address").port();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("one connection");
            let mut asked = vec![0u8; 1024];
            let read = socket.read(&mut asked).await.expect("a request");
            socket.write_all(response).await.expect("the answer");
            socket.shutdown().await.ok();
            String::from_utf8_lossy(&asked[..read]).to_string()
        });
        (port, handle)
    }

    #[tokio::test]
    async fn a_fetch_asks_for_the_token_and_carries_no_credential() {
        let (port, asked) = one_shot(
            b"HTTP/1.1 200 OK\r\n              Content-Type: text/html; charset=utf-8\r\n              Content-Security-Policy: sandbox allow-scripts; default-src 'none'\r\n              Content-Length: 13\r\n\r\n<p>panel</p>\n",
        )
        .await;

        let panel = fetch(port, "tok123").await.expect("a panel");
        let request = asked.await.expect("the listener");

        assert!(
            request.starts_with("GET /panel/tok123 HTTP/1.1\r\n"),
            "{request:?}"
        );
        // The token in the path is the credential -- see `panels.md`. A key here would be a
        // second one, and the first step towards this endpoint depending on it.
        assert!(
            !request.to_ascii_lowercase().contains("authorization"),
            "{request:?}"
        );
        assert_eq!(panel.status, 200);
        assert_eq!(panel.csp, "sandbox allow-scripts; default-src 'none'");
        assert_eq!(panel.body, b"<p>panel</p>\n");
    }

    #[tokio::test]
    async fn a_hostile_token_never_reaches_the_wire() {
        // No listener at all: the refusal has to happen before anything is dialled.
        let err = fetch(1, "abc\r\nX-Evil: 1").await.expect_err("refused");
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }
}
