//! Narrow native storage mechanism, not an application or delivery-policy engine.
//! Record bytes are opaque. SIGIL chooses their meaning and which atomic update
//! to propose. The embedding trusted host binds scopes; guests cannot mint them.

pub mod store;
