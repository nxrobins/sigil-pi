#!/bin/zsh
# sigil-pi CI — the full gate, runnable locally and by hooks.
#   ./ci.sh            run everything
# Needs: SIGIL_ROOT (default ../SIGIL) as a git checkout, cargo on PATH (step 1
# rebuilds sigil-mcp/sigil-serve at the pin — SOLVER-VERIFYING, so also a Z3 with
# headers: a Homebrew z3 is found automatically, otherwise export Z3_SYS_Z3_HEADER
# and LIBRARY_PATH; and lake via elan, since the compiler links a Lean-built
# kernel), and .venv, built from the hash-pinned lock by a python3 in the
# supported range (toolchain.py):
#   python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock
set -e
cd "$(dirname "$0")"
export SIGIL_ROOT="${SIGIL_ROOT:-$(pwd)/../SIGIL}"

echo "── 1/4 toolchain pin + rebuild ──"
# sigil-pi is forged by a binary built from a SIBLING checkout. Nothing else here
# notices when that checkout moves, so a rebuild there can turn this suite red
# with no change to sigil-pi at all (2026-07-31: 44 failures, zero local edits).
# Check the revision AND that the compiler/stdlib are clean — a binary built from
# a dirty tree corresponds to no revision at all, so the pin would be a fiction.
python3 - "$SIGIL_ROOT" <<'PY' || exit 1
import re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # ci.sh cd's to the repo root above
import toolchain

root = Path(sys.argv[1])
cfg = {}
for line in Path("SIGIL_REV").read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
note = cfg.get("note", "")

# ci.sh IS THE SOURCE-MODE GATE, deliberately: step 1 rebuilds the compiler
# with cargo and step 2 runs sigil-serve out of the checkout, neither of which
# a released binary can satisfy. It is the developer and CI gate, not the
# deployment check. The binary pin (sha256, for an installed toolchain) is
# verified where it actually applies — at agent startup, in main().
#
# ASK ABOUT CONFIGURATION, NOT ABOUT FILES. This step BUILDS the forge binary
# a few lines down (issue #6: the pin proves the source tree, so the binaries
# must be rebuilt from it rather than trusted). So nothing here may require a
# binary to exist yet — calling toolchain.resolve() did exactly that and broke
# every clean checkout, CI's included, before the build that would satisfy it.
override = toolchain.configured_override()
if override:
    sys.exit(f"FAIL: ci.sh needs a SIGIL source checkout, but {override} is set.\n"
             f"      This gate rebuilds the compiler at the pin, which only a\n"
             f"      checkout can do. Unset PI_FORGE_BIN / PI_TOOLCHAIN_DIR and\n"
             f"      set SIGIL_ROOT to run it.")

def git(*a):
    return subprocess.run(["git", "-C", str(root), *a],
                          capture_output=True, text=True).stdout.strip()

if not git("rev-parse", "HEAD"):
    sys.exit(f"FAIL: $SIGIL_ROOT ({root}) is not a git checkout")

for area in ("crates", "stdlib"):
    have = git("rev-parse", f"HEAD:{area}")
    want = cfg.get(area, "")
    if have != want:
        sys.exit(
            f"FAIL: SIGIL {area}/ tree does not match the pin.\n"
            f"        want {want}  ({note})\n"
            f"        have {have}\n"
            f"      The forge toolchain moved. sigil-pi is only known to pass at\n"
            f"      the pinned tree. Either check SIGIL out where {area}/ matches\n"
            f"      and rebuild, or update SIGIL_REV deliberately — read the\n"
            f"      tradeoffs recorded in that file first.")

# A dirty compiler/stdlib means the built binary matches no commit.
dirty = git("status", "--porcelain", "crates", "stdlib")
if dirty:
    n = len(dirty.splitlines())
    sys.exit(
        f"FAIL: $SIGIL_ROOT has {n} uncommitted change(s) under crates/ or stdlib/.\n"
        f"      The forge binary would not correspond to any revision, so the pin\n"
        f"      guarantees nothing. Commit or stash them first:\n"
        + "\n".join(f"        {l}" for l in dirty.splitlines()[:8]))
print(f"   SIGIL toolchain matches pin ({note}); crates+stdlib clean")

# The compiler is built from more than crates/ and stdlib/: SIGIL's Cargo.lock
# and Cargo.toml (each crate it links, at its version), rust-toolchain.toml (the
# rustc that compiles it) and proofs/lean (the Lean kernel and its toolchain pin,
# which sigil-formal-bridge's build.rs builds). The checks above prove none of
# them, so this gate built and tested whatever the checkout held there (audit
# 2026-11-10, W3's sweep). The release build's own check runs here, so the gate
# tests the compiler a release of this pin would ship, and the two cannot drift.
import runpy
release = runpy.run_path(str(Path.cwd() / "scripts" / "build_release.py"))
if not cfg.get("ref"):
    sys.exit("FAIL: SIGIL_REV has no `ref`, so the build inputs outside crates/ and\n"
             "      stdlib/ cannot be checked against the pinned commit.")
try:
    held = release["verify_sigil_source"](root, cfg)
except release["ReleaseBuildError"] as error:
    sys.exit(f"FAIL: {error}\n"
             f"      The compiler this gate builds would not be the pinned one. Check\n"
             f"      SIGIL out at {cfg['ref']}, or update SIGIL_REV deliberately.")
print(f"   SIGIL build inputs are the pinned ref's: {', '.join(sorted(held))}")
PY

# The pin above proves the SOURCE tree. Nothing ties the BINARIES to it: a
# leftover build from an older tree passes the pin and forges with different
# behavior (issue #6). Rebuild instead of detect — cargo is incremental, so
# this is a ~0.1s no-op when nothing moved, and afterwards the binaries are
# causally built from the tree the pin just proved.
command -v cargo >/dev/null 2>&1 || {
  echo "FAIL: cargo not found — ci.sh rebuilds sigil-mcp/sigil-serve at the pin"
  echo "      (the same cargo the README Requirements already assume)"; exit 1; }
# The compiler statically links a Lean-built kernel: sigil-formal-bridge's
# build.rs runs `lake` and panics without it, naming nothing useful ("failed to
# run pinned Lean version probe" — CI run 34015201659). Build-time only; the
# binaries need no Lean on the host. The version is SIGIL's own pin,
# $SIGIL_ROOT/proofs/lean/lean-toolchain, which elan installs on first use.
command -v lake >/dev/null 2>&1 || {
  echo "FAIL: lake not found — the pinned SIGIL compiler links a Lean-built kernel,"
  echo "      and building it runs lake. Install elan (https://github.com/leanprover/elan);"
  echo "      it installs the toolchain named in \$SIGIL_ROOT/proofs/lean/lean-toolchain."; exit 1; }
# SOLVER-VERIFYING, not the default build. A plain `cargo build -p sigil-mcp` is a
# solver-OFF compiler: the Z3 proofs (capability flow, refinement discharge) are
# skipped, and its forge gate then fails closed — R817 — unless the caller sets
# SIGIL_ALLOW_UNVERIFIED_CERT=1. SIGIL's bench harness sets that (it benchmarks
# model output; it is not a security gate). This host's vendored client STRIPS
# it, on purpose, so the binary this gate builds must be the one the product can
# actually run: built with the `solver` feature. Found 2026-08-22, when the
# merged suite went red on every forge — until then the suite forged through the
# bench client, and the product client had never forged against this binary.
#
# z3-sys finds Z3 only through Z3_SYS_Z3_HEADER (bindgen) and the linker's search
# path; it discovers neither. CI pins an official Z3 release and sets both (see
# .github/workflows/ci.yml); locally a Homebrew z3 is found here.
if [ -z "${Z3_SYS_Z3_HEADER:-}" ]; then
  if z3_prefix=$(brew --prefix z3 2>/dev/null) && [ -f "$z3_prefix/include/z3.h" ]; then
    export Z3_SYS_Z3_HEADER="$z3_prefix/include/z3.h"
    export LIBRARY_PATH="$z3_prefix/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
  elif [ -f /usr/include/z3.h ]; then
    export Z3_SYS_Z3_HEADER=/usr/include/z3.h
  fi
fi
[ -f "${Z3_SYS_Z3_HEADER:-}" ] || {
  echo "FAIL: no Z3 headers — this gate builds a SOLVER-VERIFYING sigil-mcp, which"
  echo "      needs libz3 + z3.h. Install one (brew install z3, or the pinned"
  echo "      release CI uses) and, if it is not under brew, export"
  echo "      Z3_SYS_Z3_HEADER=<path to z3.h> and LIBRARY_PATH=<dir with libz3>."; exit 1; }
( cd "$SIGIL_ROOT" && cargo build --release -p sigil-mcp -p sigil-serve --features sigil-mcp/solver,sigil-serve/solver )
echo "   forge binaries rebuilt at the pin (solver-verifying; Z3 header: $Z3_SYS_Z3_HEADER)"

echo "── 2/4 generated artifact in sync + compile gate ──"
python3 - <<'PY' || { echo "FAIL: chat_turn.sigil stale — run python3 make_chat_turn.py"; exit 1; }
from pathlib import Path
from make_chat_turn import generate
raise SystemExit(0 if Path("tools/chat_turn.sigil").read_text() == generate() else 1)
PY

tmp=$(mktemp -d)
mkdir -p "$tmp/cfg" "$tmp/sess"
# host_profile: the same declaration runtime_client.HOST_PROFILE makes on every
# forge — without it the v9 verifier refuses chat_turn (I013); see SIGIL_REV.
cat > "$tmp/check.json" <<EOF
{
  "host_profile": "ephemeral",
  "tools": {
    "chat_turn": {
      "source": "$(pwd)/tools/chat_turn.sigil",
      "fuel": 50000000,
      "grants": {"net": ["127.0.0.1"],
                 "kv": ["cfg=$tmp/cfg", "sess=$tmp/sess"],
                 "kv_write": ["sess=$tmp/sess"]}
    }
  },
  "http": {"bind": "127.0.0.1:0",
           "routes": [{"path": "/chat", "tool": "chat_turn"}]}
}
EOF
"$SIGIL_ROOT/target/release/sigil-serve" "$tmp/check.json" --check
rm -rf "$tmp"

echo "── 3/4 lint (pyflakes-level: real-bug rules only) ──"
# F (dead/shadowed imports, undefined names) + E9 (syntax errors) — the
# mechanical-slip class, zero style opinions. Same invocation as the
# standalone CI job; a guard test pins the two to stay identical.
#
# The gate runs on the locked tools or not at all. ci.sh installs nothing: a
# .venv built from an older lock, or by hand, would run it with other versions,
# or without pytest-timeout, which pytest.ini requires (audit F-16, F-17).
.venv/bin/python - <<'PY' || { echo "FAIL: .venv is missing or is not the lock — run: .venv/bin/pip install --require-hashes -r requirements-dev.lock"; exit 1; }
import re, sys
from importlib.metadata import PackageNotFoundError, version

lock = open("requirements-dev.lock").read().replace("\\\n", " ")
drift = []
for name, want in re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", lock, re.M):
    try:
        have = version(name)
    except PackageNotFoundError:
        have = "missing"
    if have != want:
        drift.append(f"{name} {have} (locked {want})")
if drift:
    sys.exit(".venv differs from requirements-dev.lock: " + ", ".join(drift))
PY
.venv/bin/python -m ruff check --select F,E9 .

echo "── 4/4 full test + independent line/branch coverage gate ──"
# The suite starts real bundles, and a bundle's launcher runs the first `python3`
# on PATH. Make that the gate's own interpreter, as setup-python already does in
# CI. On a host whose default python3 is outside the supported range the product
# now refuses to start (audit F-24); before that, the drill-fixture tests served
# real turns on that interpreter (3.15.1 on the host where F-24 was found).
export PATH="$(pwd)/.venv/bin:$PATH"
# the WHOLE tests/ tree — an enumerated list can silently skip new files.
# Each function named critical below must stay at 100% line and branch coverage;
# tests/test_guards.py and docs/product-readiness.md name the same entries, and
# no module measured here may carry a coverage pragma
# (tests/test_coverage_gate_guards.py).
.venv/bin/python -m pytest -q \
  --cov=agent --cov=product_service --cov=product_main --cov=runtime_client \
  --cov=sigil_compose --cov=state_tool --cov=toolchain \
  --cov=scripts.build_release --cov=scripts.load_test \
  --cov=scripts.check_readiness_evidence \
  --cov-branch --cov-report=
.venv/bin/python -m coverage json -o .coverage.json
.venv/bin/python scripts/check_coverage.py .coverage.json \
  --minimum-line 85 --minimum-branch 85 \
  --critical runtime_client.py \
  --critical sigil_compose.py \
  --critical product_service.py:AuthRegistry._active \
  --critical product_service.py:AuthRegistry.authenticate \
  --critical product_service.py:AuthRegistry.policy \
  --critical product_service.py:DurableQuotaStore.acquire_turn \
  --critical product_service.py:_DurableTurnLease.__init__ \
  --critical product_service.py:_DurableTurnLease.token_budget \
  --critical product_service.py:_DurableTurnLease.release \
  --critical product_service.py:_TurnTokenBudget.__init__ \
  --critical product_service.py:_TurnTokenBudget.reserve \
  --critical product_service.py:_TokenAttempt.__init__ \
  --critical product_service.py:_TokenAttempt.settle \
  --critical product_service.py:DurableQuotaStore.reserve_model_tokens \
  --critical product_service.py:DurableQuotaStore.reconcile_model_tokens \
  --critical product_service.py:DurableQuotaStore.settle_turn \
  --critical product_service.py:DurableQuotaStore.registered_session_inactive \
  --critical product_service.py:DurableQuotaStore.remove_registered_session \
  --critical product_service.py:ProductScheduleStore.internal_active \
  --critical product_service.py:ProductDataManager.verify_quota_registry \
  --critical product_service.py:ProductDataManager.export \
  --critical product_service.py:ProductDataManager.delete_internal_files \
  --critical product_service.py:ProductDataManager.delete \
  --critical product_service.py:ProductRetentionMonitor.tick \
  --critical product_service.py:_internal_session \
  --critical product_service.py:ProductService._allowed_tools \
  --critical product_service.py:ProductService.is_ready \
  --critical product_service.py:ProductService._require \
  --critical product_service.py:ProductService._validate_session \
  --critical product_service.py:ProductService._validate_message \
  --critical product_service.py:ProductService._chat_request \
  --critical product_service.py:validate_transport \
  --critical product_service.py:serve_product \
  --critical product_service.py:tls_context \
  --critical product_service.py:ProductHTTPServer.__init__ \
  --critical product_service.py:ProductHTTPServer.answering \
  --critical product_service.py:ProductHTTPServer.server_close \
  --critical product_service.py:ProductHTTPServer.get_request \
  --critical product_service.py:ProductHTTPServer.shutdown_request \
  --critical product_service.py:_readable \
  --critical product_service.py:_RequestReader.readline \
  --critical product_service.py:_ConnectionInput.__init__ \
  --critical product_service.py:_ConnectionInput.readable \
  --critical product_service.py:_ConnectionInput.readinto \
  --critical product_service.py:_declared_length \
  --critical product_service.py:drain_service \
  --critical product_service.py:ServiceMetrics.transport_error \
  --critical product_service.py:ProductService.transport_error \
  --critical product_service.py:_log_event \
  --critical product_main.py:_server_tls_context \
  --critical product_service.py:ProductService.dispatch \
  --critical product_service.py:ProductService.authenticate \
  --critical product_service.py:_request_id \
  --critical product_service.py:DurableQuotaStore.admit_request \
  --critical product_service.py:FixedWindowRateLimiter.admit \
  --critical product_service.py:ProductService._json_object \
  --critical product_service.py:ProductService._in_flight \
  --critical product_service.py:InFlightWork.enter \
  --critical product_service.py:InFlightWork.leave \
  --critical product_service.py:ProductService._turn_enter \
  --critical product_service.py:ProductService._turn_usage \
  --critical product_service.py:TenantSecrets.from_directory \
  --critical product_service.py:TenantSecrets.missing \
  --critical product_service.py:TenantSecrets.for_principal \
  --critical product_service.py:_check_private \
  --critical product_service.py:_read_secret_value \
  --critical product_service.py:principal_tool_grants \
  --critical product_service.py:ProductService._tool_grants \
  --critical agent.py:PiAgent._tool_grants \
  --critical product_service.py:ProductService.readiness_snapshot \
  --critical product_service.py:ProductService.operational_metrics \
  --critical product_service.py:render_prometheus_metrics \
  --critical product_service.py:AuditVerificationMonitor.check \
  --critical product_service.py:AuditVerificationMonitor.start \
  --critical product_service.py:AuditVerificationMonitor.stop \
  --critical product_service.py:AuditVerificationMonitor.snapshot \
  --critical product_service.py:AuditVerificationMonitor.is_healthy \
  --critical product_service.py:AuditVerificationMonitor._check_did_not_complete \
  --critical product_service.py:AuditVerificationMonitor.write_failed \
  --critical product_service.py:ProductRetentionMonitor.start \
  --critical product_service.py:ProductRetentionMonitor.stop \
  --critical product_service.py:ProductRetentionMonitor.snapshot \
  --critical product_service.py:ProductRetentionMonitor.is_healthy \
  --critical product_service.py:ProductRetentionMonitor._sweep_did_not_complete \
  --critical product_service.py:_running_thread \
  --critical product_service.py:_ended_on_its_own \
  --critical product_service.py:_audit_write_failure \
  --critical product_service.py:_report_audit_write_failure \
  --critical product_service.py:ProductScheduler.__init__ \
  --critical product_service.py:ProductScheduler._print_log \
  --critical product_service.py:ProductScheduler._due \
  --critical product_service.py:ProductScheduler._due_since \
  --critical product_service.py:ProductScheduler._halted \
  --critical product_service.py:ProductScheduler.tick \
  --critical product_service.py:ProductScheduler._keep_looking \
  --critical product_service.py:ProductScheduler._look_again \
  --critical product_service.py:ProductScheduler._look \
  --critical product_service.py:ProductScheduler._start_thread \
  --critical product_service.py:ProductScheduler._finish \
  --critical product_service.py:ProductScheduler._run_turn \
  --critical product_service.py:ProductScheduler._progressed \
  --critical product_service.py:ProductScheduler._tick_completed \
  --critical product_service.py:ProductScheduler._tick_failed \
  --critical product_service.py:ProductScheduler.is_healthy \
  --critical product_service.py:ProductScheduler.snapshot \
  --critical product_service.py:ProductScheduler.start \
  --critical product_service.py:ProductScheduler.stop \
  --critical product_service.py:ProductScheduleStore.__init__ \
  --critical product_service.py:ProductScheduleStore._locked \
  --critical product_service.py:ProductScheduleStore._tenant_hash \
  --critical product_service.py:ProductScheduleStore._key \
  --critical product_service.py:ProductScheduleStore._load \
  --critical product_service.py:ProductScheduleStore._save \
  --critical product_service.py:ProductScheduleStore.remove_scratch \
  --critical product_service.py:ProductScheduleStore.is_healthy \
  --critical product_service.py:ProductScheduleStore.put \
  --critical product_service.py:ProductScheduleStore.for_tenant \
  --critical product_service.py:ProductScheduleStore.remove \
  --critical product_service.py:ProductScheduleStore.entries \
  --critical product_service.py:ProductScheduleStore.get \
  --critical product_service.py:ProductScheduleStore.remove_session \
  --critical product_service.py:ProductScheduleStore.remove_internal \
  --critical product_service.py:ProductScheduleStore.remove_inactive \
  --critical product_service.py:ProductScheduleStore.mark_run \
  --critical product_service.py:ProductScheduleStore.claim_due \
  --critical product_service.py:ProductScheduleStore.complete_claim \
  --critical product_service.py:ProductScheduleStore.release_claim \
  --critical product_service.py:SessionOperationLocks.hold \
  --critical agent.py:PiAgent._dispatch \
  --critical agent.py:PiAgent.sandbox_for \
  --critical agent.py:PiAgent.tool_specs \
  --critical agent.py:PiAgent._session_lock \
  --critical agent.py:PiAgent._session_lock_done \
  --critical agent.py:redact_grants \
  --critical agent.py:entry_hash \
  --critical agent.py:sign_entry \
  --critical agent.py:verify_chain \
  --critical agent.py:verify_audit_dir \
  --critical agent.py:_audit_failure_replaced_by \
  --critical agent.py:AuditLog.__init__ \
  --critical agent.py:AuditLog._path \
  --critical agent.py:AuditLog.read \
  --critical agent.py:AuditLog._read_path \
  --critical agent.py:AuditLog._last_entry \
  --critical agent.py:AuditLog.append_or_raise \
  --critical agent.py:AuditLog.record \
  --critical scripts/check_readiness_evidence.py:check \
  --critical scripts/check_readiness_evidence.py:verify_attestation \
  --critical scripts/check_readiness_evidence.py:attestation_command \
  --critical scripts/check_readiness_evidence.py:_verified_parts \
  --critical scripts/check_readiness_evidence.py:_validate_turn \
  --critical scripts/check_readiness_evidence.py:_validate_online_backup \
  --critical scripts/check_readiness_evidence.py:_number \
  --critical scripts/check_readiness_evidence.py:_arguments

echo "CI PASS"
