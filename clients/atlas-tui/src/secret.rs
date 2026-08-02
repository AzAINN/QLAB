//! A value an operator typed that this client may hold and may not print.
//!
//! One type, for one journey: a credential leaves the form in `ui/views/settings`,
//! crosses the `Command` seam, is dispatched, and is spent by a single POST. Every
//! stop on that route is a place something formats what it was given —
//! `Command` derives `Debug`, the dispatcher traces every outcome, and a panic
//! message renders whatever the assertion held. A `String` would be printed in
//! full by all three, and the leak would look like ordinary diagnostics.
//!
//! So the plaintext is reachable through exactly one method, and
//! `tests/operator_gate.rs` greps the tree to keep the call site count at one:
//! the file that puts the value on the wire. Everything else can move a
//! `Secret` around and cannot read it.
//!
//! **What this does not claim.** Rust moves are copies, `String` reallocates as
//! it grows, and the allocator does not zero what it hands back — so the
//! wiping below is best effort over the buffer this code can still name, and an
//! earlier buffer a growing field left behind is memory nothing here can reach.
//! It is worth doing anyway (the live copy is the one a core dump of a
//! long-running client would carry) and it is not worth overstating.

/// A credential, carried without being readable.
///
/// `PartialEq` exists so a test can pin what a keystroke built. It compares
/// bytes and takes as long as the values agree for — this client makes no
/// constant-time claim, and the value it holds is already in the same process's
/// memory as the comparison.
#[derive(PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: String) -> Self {
        Self(value)
    }

    /// The plaintext. The one way out, and by design the one grep target.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

/// The redaction. Not `derive(Debug)`, which is the whole point: a derived one
/// would print the field, and every `?` in this crate would print it with it.
impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // No length either. It is not much, but it is the one thing about a
        // secret that is free to leak and never useful to a reader.
        f.write_str("Secret(<redacted>)")
    }
}

/// Overwrite what is still reachable before it is dropped.
///
/// `String` deallocation does not zero, so a dropped credential sits in freed
/// memory until something else claims it. This writes over the live buffer
/// first — same length, so the write lands in the same allocation rather than
/// moving the text somewhere new and leaving the original behind.
impl Drop for Secret {
    fn drop(&mut self) {
        wipe(&mut self.0);
    }
}

/// Best-effort overwrite of a plaintext buffer, then empty it.
///
/// Shared with the form, which holds the two fields as `String`s while they are
/// being typed into: one definition, so "cleared" means the same thing on the
/// key path and on the wire.
pub fn wipe(text: &mut String) {
    if text.is_empty() {
        return;
    }
    // Byte-for-byte, so the replacement lands in the buffer that is already
    // there. `\0` is valid UTF-8, so this stays a `String` throughout.
    let over = "\0".repeat(text.len());
    text.replace_range(.., &over);
    text.clear();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_secret_prints_as_a_redaction_and_still_holds_what_it_was_given() {
        let secret = Secret::new("s3cret/abcdefghijklmnopqrstuv".into());
        assert_eq!(format!("{secret:?}"), "Secret(<redacted>)");
        // Inside a container, which is how it actually travels: a `Debug` that
        // only redacted at the top level would leak the moment it was a field.
        assert!(!format!("{:?}", Some(&secret)).contains("s3cret"));
        assert_eq!(secret.expose(), "s3cret/abcdefghijklmnopqrstuv");
    }

    #[test]
    fn wiping_a_field_covers_the_buffer_rather_than_moving_the_text_elsewhere() {
        // What is left behind cannot be read back from safe code, so this pins
        // the property that would break first: an implementation that built the
        // replacement somewhere new would leave the original allocation holding
        // the credential and pass a length check regardless.
        let mut typed = String::from("PKTEST0123456789");
        let before = typed.as_ptr();
        wipe(&mut typed);
        assert!(typed.is_empty());
        assert_eq!(
            typed.as_ptr(),
            before,
            "the overwrite moved the text instead of covering it"
        );
        // Idempotent: `Esc` on an already-cleared form is not a special case.
        wipe(&mut typed);
        assert!(typed.is_empty());
    }
}
