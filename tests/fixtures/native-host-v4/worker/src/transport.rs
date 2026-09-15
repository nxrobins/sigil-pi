//! Nonblocking Unix child transport. No retries and no application verdicts.
use crate::{Fault, strict};
use serde_json::Value;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub const FRAME_MAX: usize = 16 * 1024 * 1024;
const STDERR_MAX: usize = 256 * 1024;

fn nonblocking(fd: RawFd) -> Result<(), Fault> {
    // SAFETY: fd is a live owned pipe. This changes flags, never ownership.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(Fault::Transport);
    }
    Ok(())
}

pub struct Worker {
    child: Child,
    input: Option<ChildStdin>,
    output: ChildStdout,
    error: ChildStderr,
    stderr_bytes: usize,
    stderr_eof: bool,
    reaped: bool,
    owner: u32,
}
impl Worker {
    pub fn spawn(command: &mut Command) -> Result<Self, Fault> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        #[cfg(target_os = "linux")]
        {
            let parent = std::process::id() as libc::pid_t;
            // SAFETY: the child hook calls only the following libc operations;
            // no allocation, locks, environment access, or application code.
            unsafe {
                command.pre_exec(move || {
                    if libc::prctl(
                        libc::PR_SET_PDEATHSIG,
                        libc::SIGKILL as libc::c_ulong,
                        0 as libc::c_ulong,
                        0 as libc::c_ulong,
                        0 as libc::c_ulong,
                    ) < 0
                    {
                        return Err(io::Error::last_os_error());
                    }
                    if libc::getppid() != parent {
                        libc::_exit(127);
                    }
                    Ok(())
                });
            }
        }
        let mut child = command.spawn().map_err(|_| Fault::Spawn)?;
        // Command was configured with piped stdio; these handles are guaranteed.
        let input = child.stdin.take().expect("piped stdin");
        let output = child.stdout.take().expect("piped stdout");
        let error = child.stderr.take().expect("piped stderr");
        let mut worker = Self {
            child,
            input: Some(input),
            output,
            error,
            stderr_bytes: 0,
            stderr_eof: false,
            reaped: false,
            owner: std::process::id(),
        };
        if let Err(e) = nonblocking(worker.input.as_ref().unwrap().as_raw_fd())
            .and_then(|_| nonblocking(worker.output.as_raw_fd()))
            .and_then(|_| nonblocking(worker.error.as_raw_fd()))
        {
            if !worker.stop() {
                return Err(Fault::CleanupUnconfirmed);
            }
            return Err(e);
        }
        Ok(worker)
    }

    pub fn exchange(
        &mut self,
        request: &Value,
        id: u64,
        deadline: Instant,
        cancel: &AtomicBool,
        may_have_run: &mut bool,
    ) -> Result<Value, Fault> {
        let mut wire = serde_json::to_vec(request).map_err(|_| Fault::Protocol)?;
        if wire.len() >= FRAME_MAX {
            return Err(Fault::Limit);
        }
        wire.push(b'\n');
        let mut sent = 0;
        let mut output = Vec::new();
        let mut buffer = [0u8; 8192];
        loop {
            if self.owner != std::process::id() {
                return Err(Fault::WrongProcess);
            }
            if cancel.load(Ordering::Acquire) {
                return Err(Fault::Cancelled);
            }
            if Instant::now() >= deadline {
                return Err(Fault::Deadline);
            }
            if sent < wire.len() {
                match self
                    .input
                    .as_mut()
                    .ok_or(Fault::Transport)?
                    .write(&wire[sent..])
                {
                    Ok(0) => return Err(Fault::Transport),
                    Ok(n) => {
                        sent += n;
                        *may_have_run = true;
                    }
                    Err(e)
                        if matches!(
                            e.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                        ) => {}
                    Err(_) => return Err(Fault::Transport),
                }
            }
            // Bound work per iteration; continuous stderr cannot starve the timer.
            match self.error.read(&mut buffer) {
                Ok(0) => self.stderr_eof = true,
                Ok(n) => {
                    self.stderr_bytes += n;
                    if self.stderr_bytes > STDERR_MAX {
                        return Err(Fault::StderrLimit);
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err(Fault::Transport),
            }
            match self.output.read(&mut buffer) {
                Ok(0) => return Err(Fault::Transport),
                Ok(n) => {
                    if output.len() + n > FRAME_MAX {
                        return Err(Fault::Limit);
                    }
                    let previous = output.len();
                    output.extend_from_slice(&buffer[..n]);
                    if let Some(offset) = buffer[..n].iter().position(|b| *b == b'\n') {
                        let end = previous + offset;
                        if end + 1 != output.len() || sent != wire.len() {
                            return Err(Fault::Protocol);
                        }
                        let response =
                            strict::parse(&output[..end]).map_err(|_| Fault::Protocol)?;
                        let obj = response.as_object().ok_or(Fault::Protocol)?;
                        if obj
                            .keys()
                            .any(|k| !matches!(k.as_str(), "jsonrpc" | "id" | "result" | "error"))
                            || response["jsonrpc"] != "2.0"
                            || response["id"].as_u64() != Some(id)
                            || obj.contains_key("result") == obj.contains_key("error")
                        {
                            return Err(Fault::Protocol);
                        }
                        if obj.contains_key("error") {
                            return Err(Fault::RemoteProtocol);
                        }
                        if cancel.load(Ordering::Acquire) {
                            return Err(Fault::Cancelled);
                        }
                        if Instant::now() >= deadline {
                            return Err(Fault::Deadline);
                        }
                        return Ok(response["result"].clone());
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err(Fault::Transport),
            }
            let mut poll = [
                libc::pollfd {
                    fd: self.output.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: if self.stderr_eof {
                        -1
                    } else {
                        self.error.as_raw_fd()
                    },
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: if sent < wire.len() {
                        self.input.as_ref().unwrap().as_raw_fd()
                    } else {
                        -1
                    },
                    events: if sent < wire.len() { libc::POLLOUT } else { 0 },
                    revents: 0,
                },
            ];
            let ms = deadline
                .saturating_duration_since(Instant::now())
                .as_millis()
                .min(20) as i32;
            // SAFETY: pointer/length refer to this initialized stack array.
            let rc = unsafe { libc::poll(poll.as_mut_ptr(), poll.len() as libc::nfds_t, ms) };
            if rc < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
                return Err(Fault::Transport);
            }
        }
    }

    pub fn stop(&mut self) -> bool {
        if self.owner != std::process::id() {
            return false;
        }
        if self.reaped {
            return true;
        }
        let pid = self.child.id() as libc::pid_t;
        // Signal once BEFORE reaping: the owned, unreaped leader prevents PID
        // reuse. Never send a group signal using a subsequently recycled PID.
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
        let _ = self.child.kill();
        self.input.take();
        let until = Instant::now() + Duration::from_secs(2);
        while Instant::now() < until {
            match self.child.try_wait() {
                Ok(Some(_)) => {
                    self.reaped = true;
                    return true;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(2)),
                Err(_) => return false,
            }
        }
        false
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.stop();
    }
}
