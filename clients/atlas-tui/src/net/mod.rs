//! Everything that leaves the process. The owner runtime is the only host this
//! client may talk to — it holds the sole registry handle, and this crate has none.

pub mod http;
pub mod sse;
