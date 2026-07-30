//! The owner runtime is the only thing this client talks to.
//!
//! Invariant 1 says the owner HTTP runtime is the single DuckDB writer, and
//! every other surface reaches the registry over HTTP. That is not a Python
//! detail — it is why a second client in a second language is safe to build at
//! all. This module is the whole boundary: no file paths, no database handle,
//! no way to acquire either.

use anyhow::{anyhow, Result};
use serde_json::Value;
use std::time::Duration;

/// Long enough to ride out an owner that is mid-valuation, short enough that a
/// dead owner is reported rather than hung on.
const TIMEOUT: Duration = Duration::from_secs(8);

pub struct OwnerClient {
    base: String,
    agent: ureq::Agent,
}

/// Whether the owner is reachable, and what it said if not.
///
/// Kept as a value rather than a bare `bool` because the reason is the useful
/// half: "connection refused" and "404" mean different fixes, and a client that
/// renders an empty desk for both teaches the operator nothing.
#[derive(Debug, Clone)]
pub enum Readiness {
    Ready,
    Unreachable(String),
}

impl Readiness {
    pub fn is_ready(&self) -> bool {
        matches!(self, Readiness::Ready)
    }

    pub fn reason(&self) -> &str {
        match self {
            Readiness::Ready => "",
            Readiness::Unreachable(why) => why,
        }
    }
}

impl OwnerClient {
    pub fn new(base: impl Into<String>) -> Self {
        let agent = ureq::AgentBuilder::new()
            .timeout_read(TIMEOUT)
            .timeout_connect(TIMEOUT)
            .build();
        Self { base: base.into().trim_end_matches('/').to_string(), agent }
    }

    /// Resolve the owner URL the same way every other qlab surface does, so a
    /// desk started on a non-default port is found without extra flags.
    pub fn from_env() -> Self {
        let port = std::env::var("QLAB_UI_PORT")
            .ok()
            .and_then(|p| p.trim().parse::<u16>().ok())
            .unwrap_or(8765);
        Self::new(format!("http://127.0.0.1:{port}"))
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    /// Probe before rendering. A client that opens onto a blank frame and only
    /// then discovers there is no owner has already lied to the operator once.
    pub fn readiness(&self) -> Readiness {
        match self.agent.get(&format!("{}/readyz", self.base)).call() {
            Ok(_) => Readiness::Ready,
            Err(ureq::Error::Status(code, _)) => {
                Readiness::Unreachable(format!("owner answered {code}"))
            }
            Err(err) => Readiness::Unreachable(format!(
                "no owner on {} ({err}) — start one with `qlab tui` or `qlab ui`",
                self.base
            )),
        }
    }

    /// The one consistent terminal snapshot. Deliberately the same endpoint the
    /// Textual client uses: two clients disagreeing about the desk would be
    /// worse than having only one.
    pub fn snapshot(&self, offline: bool) -> Result<Value> {
        let url = format!("{}/api/tui?offline={}", self.base, if offline { 1 } else { 0 });
        let value: Value = self
            .agent
            .get(&url)
            .call()
            .map_err(|e| anyhow!("snapshot request failed: {e}"))?
            .into_json()
            .map_err(|e| anyhow!("snapshot was not JSON: {e}"))?;
        Ok(value)
    }
}

/// Read a dotted path out of a snapshot without unwrapping through five levels.
///
/// The owner is free to omit keys — a desk with no coordinator has no
/// `coordinator` object at all — so every read here is a miss, not a panic.
pub fn dig<'a>(root: &'a Value, path: &str) -> Option<&'a Value> {
    let mut node = root;
    for key in path.split('.') {
        node = node.get(key)?;
    }
    Some(node)
}

pub fn dig_str(root: &Value, path: &str) -> Option<String> {
    dig(root, path).and_then(|v| v.as_str()).map(|s| s.to_string())
}

pub fn dig_f64(root: &Value, path: &str) -> Option<f64> {
    dig(root, path).and_then(|v| v.as_f64())
}

pub fn dig_bool(root: &Value, path: &str) -> Option<bool> {
    dig(root, path).and_then(|v| v.as_bool())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dig_walks_a_dotted_path() {
        let v = json!({"atlas": {"mode": "research", "open_tasks": 2}});
        assert_eq!(dig_str(&v, "atlas.mode").as_deref(), Some("research"));
        assert_eq!(dig_f64(&v, "atlas.open_tasks"), Some(2.0));
    }

    #[test]
    fn a_missing_key_is_a_miss_not_a_panic() {
        // The owner omits whole objects for state that does not exist yet, so
        // absence has to be ordinary.
        let v = json!({"atlas": {}});
        assert!(dig(&v, "atlas.coordinator.driving").is_none());
        assert!(dig_bool(&v, "nothing.here").is_none());
    }

    #[test]
    fn readiness_carries_the_reason() {
        let r = Readiness::Unreachable("no owner on :8765".into());
        assert!(!r.is_ready());
        assert!(r.reason().contains("8765"));
        assert!(Readiness::Ready.reason().is_empty());
    }

    #[test]
    fn the_port_comes_from_the_same_env_var_the_rest_of_qlab_uses() {
        // Not a cosmetic default: a desk on a non-default port must be found by
        // every client, or one of them opens a second registry writer.
        std::env::set_var("QLAB_UI_PORT", "9123");
        assert_eq!(OwnerClient::from_env().base(), "http://127.0.0.1:9123");
        std::env::remove_var("QLAB_UI_PORT");
    }
}
