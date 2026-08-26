//! The terminal clipboard, through the terminal.
//!
//! This client captures the mouse — the rail is clickable and the chat
//! scrolls — and a captured mouse is a terminal that no longer selects text.
//! OSC 52 is the answer the terminal protocol itself provides: the program
//! hands the terminal a base64 payload and the terminal puts it on the
//! system clipboard. iTerm2, kitty, WezTerm, Alacritty, foot and Windows
//! Terminal honour it; Terminal.app does not, and the hint row says so by
//! naming the fallback (Shift-drag selects through a captured mouse in most
//! terminals).
//!
//! Hand-rolled base64, because a dependency for twenty lines of table
//! lookup is a supply-chain surface bought for nothing.

use std::io::Write;

const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard base64 with `=` padding, RFC 4648.
pub fn base64(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// The OSC 52 sequence that sets the system clipboard to `text`.
pub fn osc52(text: &str) -> String {
    format!("\x1b]52;c;{}\x07", base64(text.as_bytes()))
}

/// Put `text` on the system clipboard through the terminal. Fallible and
/// said so by the caller: a terminal that ignores OSC 52 ignores it silently,
/// which is why the caller names the fallback rather than claiming success.
pub fn copy(text: &str) -> std::io::Result<()> {
    let mut out = std::io::stdout().lock();
    out.write_all(osc52(text).as_bytes())?;
    out.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_the_rfc_vectors() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64(b"9cc4fcf0"), "OWNjNGZjZjA=");
    }

    #[test]
    fn the_sequence_is_osc_52_on_the_clipboard_selection() {
        let seq = osc52("abc");
        assert!(seq.starts_with("\x1b]52;c;"), "{seq:?}");
        assert!(seq.ends_with('\x07'), "{seq:?}");
        assert!(seq.contains("YWJj"), "{seq:?}");
    }
}
