#!/bin/zsh
# sigil-pi CI — the full gate, runnable locally and by hooks.
#   ./ci.sh            run everything
# Needs: SIGIL_ROOT (default ../SIGIL) with target/release/{sigil-mcp,sigil-serve}
# built, and .venv (python3 -m venv .venv && .venv/bin/pip install pytest hypothesis).
set -e
cd "$(dirname "$0")"
export SIGIL_ROOT="${SIGIL_ROOT:-$(pwd)/../SIGIL}"

echo "── 0/3 toolchain pin ──"
# sigil-pi is forged by a binary built from a SIBLING checkout. Nothing else here
# notices when that checkout moves, so a rebuild there can turn this suite red
# with no change to sigil-pi at all (2026-07-31: 44 failures, zero local edits).
# Check the revision AND that the compiler/stdlib are clean — a binary built from
# a dirty tree corresponds to no revision at all, so the pin would be a fiction.
python3 - "$SIGIL_ROOT" <<'PY' || exit 1
import re, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
cfg = {}
for line in Path("SIGIL_REV").read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
note = cfg.get("note", "")

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
PY

echo "── 1/3 generated artifact in sync + compile gate ──"
python3 - <<'PY' || { echo "FAIL: chat_turn.sigil stale — run python3 make_chat_turn.py"; exit 1; }
from pathlib import Path
from make_chat_turn import generate
raise SystemExit(0 if Path("tools/chat_turn.sigil").read_text() == generate() else 1)
PY

tmp=$(mktemp -d)
mkdir -p "$tmp/cfg" "$tmp/sess"
cat > "$tmp/check.json" <<EOF
{
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

echo "── 3/3 full test suite (property, guards, integration, sweep, dispatch) ──"
# the WHOLE tests/ tree — an enumerated list can silently skip new files
.venv/bin/python -m pytest -q

echo "CI PASS"
