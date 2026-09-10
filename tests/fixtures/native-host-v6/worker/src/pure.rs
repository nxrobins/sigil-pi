//! Fixed GRANTLESS evaluator with one bounded reusable runtime process. The
//! pinned forge still verifies SIGIL on every call and creates a fresh guest
//! Store/Instance; only its immutable compiled-module cache survives. This is
//! deliberately separate from one-use effect tickets and effect observations.
use super::*;

const CALL_LIMIT: u64 = 4096;
pub struct PureBridge {
    admitted: Bridge,
    worker: Option<Worker>,
    calls: u64,
}
impl PureBridge {
    pub fn new(config: Config) -> Result<Self, Fault> {
        if !config.net.is_empty() || !config.fs.is_empty() || !config.secret_env.is_empty() {
            return Err(Fault::Invalid);
        }
        Ok(Self {
            admitted: Bridge::new(config)?,
            worker: None,
            calls: 0,
        })
    }

    fn retire(&mut self) -> Result<(), Fault> {
        if let Some(mut worker) = self.worker.take()
            && !worker.stop()
        {
            self.admitted.poisoned = true;
            return Err(Fault::CleanupUnconfirmed);
        }
        self.calls = 0;
        Ok(())
    }

    /// No source, grants, receipts or result claims are accepted from callers.
    /// A failed/uncertain evaluation is not automatically sent again. The native
    /// owner decides whether to invoke its fixed pure policy on fresh facts later.
    pub fn invoke(&mut self, input: String, fuel: u64, timeout_ms: u64) -> Result<Value, Fault> {
        self.admitted.live()?;
        if input.len() > 4 * 1024 * 1024
            || fuel == 0
            || fuel > self.admitted.config.max_fuel
            || timeout_ms == 0
            || timeout_ms > self.admitted.config.max_timeout_ms.min(30000)
        {
            return Err(Fault::Limit);
        }
        let deadline = Instant::now() + Duration::from_millis(timeout_ms);
        if self.calls == CALL_LIMIT {
            self.retire()?;
        }
        if pinned_file(
            &self.admitted.config.runtime,
            &self.admitted.config.runtime_sha256,
            256 * 1024 * 1024,
        )
        .is_err()
        {
            self.retire()?;
            return Err(Fault::Invalid);
        }
        let result = self.evaluate(input, fuel, deadline);
        if result.is_err() {
            self.retire()?;
        }
        result
    }

    fn evaluate(&mut self, input: String, fuel: u64, deadline: Instant) -> Result<Value, Fault> {
        let cancel = AtomicBool::new(false);
        if Instant::now() >= deadline {
            return Err(Fault::Deadline);
        }
        if self.worker.is_none() {
            let mut command = Command::new(&self.admitted.config.runtime);
            command.env_clear().env("LANG", "C.UTF-8").current_dir("/");
            self.worker = match Worker::spawn(&mut command) {
                Ok(worker) => Some(worker),
                Err(fault) => {
                    if fault == Fault::CleanupUnconfirmed {
                        self.admitted.poisoned = true;
                    }
                    return Err(fault);
                }
            };
            let mut sent = false;
            self.worker.as_mut().unwrap().exchange(
                &json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
                1,
                deadline.min(Instant::now() + Duration::from_secs(5)),
                &cancel,
                &mut sent,
            )?;
        }
        self.calls += 1;
        let id = self.calls + 1;
        let mut sent = false;
        let result = self.worker.as_mut().unwrap().exchange(
            &json!({"jsonrpc":"2.0","id":id,"method":"tools/call","params":{
                "name":"sigil_forge","arguments":{"source":self.admitted.source,"input":input,
                    "fuel":fuel,"host_profile":"ephemeral","grants":{"net":[],"fs":[],"secret":[]}}}}),
            id, deadline, &cancel, &mut sent)?;
        let content = result["content"].as_array().ok_or(Fault::Protocol)?;
        if content.len() != 1 || content[0]["type"] != "text" || result["isError"] == true {
            return Err(Fault::Protocol);
        }
        let inner = strict::parse(
            content[0]["text"]
                .as_str()
                .ok_or(Fault::Protocol)?
                .as_bytes(),
        )
        .map_err(|_| Fault::Protocol)?;
        if !matches!(inner["status"].as_str(), Some("ok" | "error")) {
            return Err(Fault::Protocol);
        }
        if Instant::now() >= deadline {
            return Err(Fault::Deadline);
        }
        Ok(inner)
    }
}
impl Drop for PureBridge {
    fn drop(&mut self) {
        let _ = self.retire();
    }
}

#[cfg(test)]
mod tests;
