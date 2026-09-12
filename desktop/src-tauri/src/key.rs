//! The access key: minted here, spent in exactly two places, written down nowhere.
//!
//! The gateway is a credential store that spawns subprocesses, so a socket anyone on the
//! machine can open is both arbitrary code execution and a credential oracle. Loopback is
//! not a boundary between *accounts* on the same machine, which is why `gateway.env.example`
//! recommends a key even for a loopback bind.
//!
//! The shell mints one per launch. It reaches the gateway through the environment -- never
//! argv, which is world-readable through `ps`, a rule this project states in
//! `gateway.env.example` and follows in the Makefile's `run` target -- and it reaches the
//! sockets as an `Authorization: Bearer` header. It is never serialised into a Tauri
//! command's arguments or an event payload, so the webview cannot read it even though the
//! webview is the thing it exists to let in.
//!
//! Per launch rather than per restart of the child: the proxy holds sockets across a
//! gateway restart, and rotating the key underneath them would mean a renegotiation with
//! nothing to gain. The key's lifetime is the app's.
//!
//! There is deliberately no `Drop` that zeroes the bytes. `String` moves in Rust leave
//! copies behind, the allocator reuses pages, and the process this is protecting against
//! having compromised has already won; a scrub here would be reassurance rather than
//! defence.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;

/// The environment variable `transport_ws.resolve_access_key` reads first.
///
/// It takes precedence over `WS_ACCESS_KEY` in `gateway.env`, which matters: a user who
/// later pastes a key into the seeded credential file must not be able to lock the app out
/// of the daemon it spawned. Pinned from the Python side by
/// `tests/test_desktop_contract.py::test_the_environment_beats_the_credential_file`.
pub const ACCESS_KEY_ENV: &str = "MCP_GATEWAY_WS_KEY";

/// 32 bytes, which is what `gateway.env.example` tells a human to generate.
const KEY_BYTES: usize = 32;

/// A minted access key.
///
/// No `Debug`, no `Display`, no `Serialize`, no `Clone`. Those are not omissions: each one
/// would be a way for this value to reach a log line, an error message, or the webview.
pub struct AccessKey(String);

impl AccessKey {
    /// Mint one from the OS's randomness.
    ///
    /// Panics if the OS cannot supply randomness, which is not a condition to carry on
    /// through: the alternative is a predictable key on a socket that reaches every
    /// credential on the machine.
    pub fn mint() -> Self {
        let mut bytes = [0u8; KEY_BYTES];
        getrandom::getrandom(&mut bytes).expect("the OS has no randomness; refusing to guess");
        Self(URL_SAFE_NO_PAD.encode(bytes))
    }

    /// The value for `MCP_GATEWAY_WS_KEY` in the child's environment.
    pub(crate) fn as_env(&self) -> &str {
        &self.0
    }

    /// The `Authorization` header value the proxy dials with.
    pub(crate) fn header(&self) -> String {
        format!("Bearer {}", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_key_is_url_safe_so_it_survives_every_carrier_it_might_meet() {
        let key = AccessKey::mint();
        assert!(
            key.as_env()
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
            "{}",
            key.as_env()
        );
    }

    #[test]
    fn a_key_carries_the_entropy_it_claims_to() {
        // 32 bytes, base64url without padding, is 43 characters.
        assert_eq!(AccessKey::mint().as_env().len(), 43);
    }

    #[test]
    fn two_keys_are_not_the_same_key() {
        assert_ne!(AccessKey::mint().as_env(), AccessKey::mint().as_env());
    }

    #[test]
    fn the_header_is_the_bearer_form_transport_ws_parses() {
        let key = AccessKey::mint();
        // `_offered_keys` lowercases and strips `bearer `, then compares what is left.
        assert_eq!(key.header(), format!("Bearer {}", key.as_env()));
    }
}
