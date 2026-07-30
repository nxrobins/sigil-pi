#!/bin/zsh
# sigil-pi CI — the full gate, runnable locally and by hooks.
#   ./ci.sh            run everything
# Needs: SIGIL_ROOT (default ../SIGIL) with target/release/{sigil-mcp,sigil-serve}
# built, and .venv (python3 -m venv .venv && .venv/bin/pip install pytest hypothesis).
set -e
cd "$(dirname "$0")"
export SIGIL_ROOT="${SIGIL_ROOT:-$(pwd)/../SIGIL}"

echo "── 1/2 generated artifact in sync + compile gate ──"
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

echo "── 2/2 full test suite (property, guards, integration, sweep, dispatch) ──"
# the WHOLE tests/ tree — an enumerated list can silently skip new files
.venv/bin/python -m pytest -q

echo "CI PASS"
