"""Where the SIGIL toolchain is — the one seam between sigil-pi and it.

WHY THIS FILE EXISTS
sigil-pi is not self-contained: every tool is forged by a sigil-mcp binary
built from SIGIL, and every guest is composed against SIGIL's stdlib source.
Until now that dependency was spelled four different ways in four files — a
`sys.path.insert` in agent.py, another in tests/conftest.py, another in
make_chat_turn.py, and a `target/release/...` path derived from SIGIL_ROOT in
each of them. Every one of those spellings assumes a SOURCE CHECKOUT of a repo
not everyone can clone, which is why sigil-pi could not be installed by anyone
outside it (issue #15).

This module is the seam. It answers three questions — where is the forge
binary, where is the stdlib, and what revision are they — and answers them from
any of three arrangements. Today it resolves to a source checkout. The day
SIGIL ships a released binary it resolves to that instead, and NOTHING ABOVE
THIS FILE CHANGES. That is the whole point: the arrangement is a deployment
detail, not an architectural one.

RESOLUTION ORDER (first match wins; every path tried is named on failure)

  1. EXPLICIT      PI_FORGE_BIN / PI_SERVE_BIN / PI_STDLIB_DIR
                   An operator pointing at exactly what they mean. Highest
                   precedence because an explicit answer must never lose to a
                   checkout that happens to be lying around.

  2. RELEASE       PI_TOOLCHAIN_DIR, else ~/.cache/sigil-pi/<rev>
                   layout: bin/sigil-mcp, bin/sigil-serve, stdlib/
                   The shape a published SIGIL would install into. Nothing
                   writes this directory yet; resolving from it already works,
                   so the path is testable before the day it matters.

  3. SOURCE        SIGIL_ROOT (default ../SIGIL) — target/release/sigil-mcp
                   Today's behaviour, and the developer path forever.

THE STDLIB IS NOT OPTIONAL. It is tempting to think the binary alone is enough
to run the agent, because the composed guests look like build artifacts. They
are not: PiAgent composes agent_turn and parse_reply at construction time and
each shape tool at dispatch time (agent.py), so `compose_with_stdlib` runs on
the hot path of a live deployment. A release that ships the binary without the
stdlib would import fine, serve fine, and fail on the first turn — so this
module refuses to resolve at all unless it finds both.

WHAT THIS MODULE DOES NOT DO
It does not download anything. Fetching a release is a separate decision with
its own trust question (what verifies the bytes?), and quietly reaching the
network from a resolver would be exactly the kind of ambient authority this
project exists to argue against. `verify_binary` is here for when that day
comes; wiring is deliberately absent.
"""
import hashlib
import os
import platform
import sys
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent
REV_FILE = PI_ROOT / "SIGIL_REV"

# The env var that turns a missing toolchain from a skip into an error. Set by
# the forge CI job; see tests/conftest.py for why that distinction is
# load-bearing rather than a convenience.
REQUIRE_ENV = "PI_REQUIRE_TOOLCHAIN"


class ToolchainNotFound(RuntimeError):
    """No toolchain at any known location. Carries every path tried, because
    'toolchain not found' without the search list is a bug report nobody can
    act on — the same reason SIGIL_REV exists at all."""


def read_rev(path: Path = REV_FILE) -> dict:
    """SIGIL_REV as {key: value}. Blank lines and `#` comments ignored.

    Mirrors the parser in ci.sh and tests/test_guards.py rather than replacing
    them: a guard that imports the code it is guarding proves less."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def platform_key() -> str:
    """The suffix of the `sha256_<platform>` key in SIGIL_REV for THIS host.

    A binary pin is per-platform — one sha256 line cannot describe a macOS
    arm64 build and a linux x86_64 one, and a pin that silently matches the
    wrong platform is worse than no pin."""
    machine = platform.machine().lower()
    # x86_64 and amd64 are the same thing under two names; normalising here
    # keeps SIGIL_REV from needing an alias for every OS's spelling.
    if machine in ("amd64", "x86_64"):
        machine = "x86_64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64"
    system = "macos" if sys.platform == "darwin" else sys.platform
    return f"{system}_{machine}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_binary(path: Path, rev: dict = None) -> str:
    """Check a forge binary against the `sha256_<platform>` pin in SIGIL_REV.

    Returns a human-readable problem, or None when the binary matches or when
    no pin exists for this platform. NOT having a pin is not a failure: the
    tree-hash pin remains the authority for a source checkout, and a released
    binary for a platform nobody has published yet must not be treated as
    tampered-with. Silence here means 'nothing to check', and the caller is
    expected to know which pin mode applies — see ci.sh step 1."""
    rev = read_rev() if rev is None else rev
    want = rev.get(f"sha256_{platform_key()}")
    if not want:
        return None
    if not path.exists():
        return f"{path} does not exist"
    got = sha256_file(path)
    if got != want:
        return (f"{path} does not match the SIGIL_REV pin for "
                f"{platform_key()}:\n  pinned {want}\n  actual {got}")
    return None


class Toolchain:
    """A resolved toolchain: the binaries, the stdlib, and where they came
    from. `origin` is carried for diagnostics — when a forge misbehaves, the
    first question is always which toolchain actually ran."""

    def __init__(self, forge_bin: Path, serve_bin: Path, stdlib_repo: Path,
                 origin: str):
        self.forge_bin = forge_bin
        self.serve_bin = serve_bin
        # The path handed to compose_with_stdlib. It is a REPO ROOT, not the
        # stdlib directory itself — compose looks for `stdlib/` beneath it —
        # so a release layout puts its stdlib at <release>/stdlib/ and passes
        # <release>. Keeping compose's argument shape unchanged means the
        # release path exercises exactly the code the checkout path does.
        self.stdlib_repo = stdlib_repo
        self.origin = origin

    def __repr__(self):
        return f"<Toolchain {self.origin}: {self.forge_bin}>"


def _explicit():
    forge = os.environ.get("PI_FORGE_BIN")
    if not forge:
        return None, []
    forge = Path(forge).expanduser().resolve()
    stdlib = os.environ.get("PI_STDLIB_DIR")
    if not stdlib:
        # Explicit-but-incomplete is an operator mistake worth naming loudly,
        # not something to paper over by falling through to a checkout that
        # may be a different revision entirely.
        raise ToolchainNotFound(
            "PI_FORGE_BIN is set but PI_STDLIB_DIR is not. The agent composes "
            "guests against the stdlib on every turn, so the binary alone is "
            "not a usable toolchain. Set both, or unset PI_FORGE_BIN to fall "
            "back to a source checkout.")
    serve = os.environ.get("PI_SERVE_BIN")
    serve = (Path(serve).expanduser().resolve() if serve
             else forge.parent / "sigil-serve")
    tried = [forge]
    if not forge.exists():
        return None, tried
    return Toolchain(forge, serve, Path(stdlib).expanduser().resolve(),
                     "explicit (PI_FORGE_BIN)"), tried


def _release():
    root = os.environ.get("PI_TOOLCHAIN_DIR")
    if root:
        roots = [Path(root).expanduser().resolve()]
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME",
                                    Path.home() / ".cache")) / "sigil-pi"
        rev = read_rev().get("ref", "")
        # Keyed by revision so two pins can coexist on one machine — bisecting
        # a toolchain regression should not mean re-downloading each way.
        roots = [cache / rev] if rev else []
    tried = []
    for base in roots:
        forge = base / "bin" / "sigil-mcp"
        tried.append(forge)
        if forge.exists() and (base / "stdlib").is_dir():
            return Toolchain(forge, base / "bin" / "sigil-serve", base,
                             f"release ({base})"), tried
    return None, tried


def _source():
    root = Path(os.environ.get("SIGIL_ROOT",
                               PI_ROOT.parent / "SIGIL")).expanduser().resolve()
    forge = root / "target" / "release" / "sigil-mcp"
    if forge.exists():
        return Toolchain(forge, root / "target" / "release" / "sigil-serve",
                         root, f"source checkout ({root})"), [forge]
    return None, [forge]


def resolve(require: bool = True):
    """The resolved Toolchain, or None when there is none and `require` is
    False. Raises ToolchainNotFound — naming every path tried — otherwise.

    `require=False` exists for the test suite and for `--verify-audit`: both
    are legitimately toolchain-free, and neither should be blocked from
    running by a missing compiler it never intended to use."""
    tried = []
    for probe in (_explicit, _release, _source):
        found, paths = probe()
        tried.extend(paths)
        if found is not None:
            return found
    if not require:
        return None
    listing = "\n".join(f"  {p}" for p in tried)
    raise ToolchainNotFound(
        "no SIGIL toolchain found. Tried, in order:\n"
        f"{listing}\n\n"
        "Set PI_FORGE_BIN + PI_STDLIB_DIR to point at a built toolchain, or "
        "SIGIL_ROOT at a checkout with `cargo build --release -p sigil-mcp` "
        "already run. See the Requirements section of the README.")


def available() -> bool:
    """Whether a toolchain resolves at all. Cheap enough to call per-fixture."""
    try:
        return resolve(require=False) is not None
    except ToolchainNotFound:
        # An explicit-but-incomplete configuration. Reporting that as "not
        # available" would let a typo masquerade as an unconfigured machine
        # and silently skip the forge tests, so it stays an error.
        raise


def required() -> bool:
    """Whether a missing toolchain must be an ERROR rather than a skip.

    The forge CI job sets this. Without it, a suite that quietly skips every
    real test reports the same green as one that ran them — the precise
    failure ci.yml's header already documents at the job level, one layer
    down."""
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def client():
    """The SIGIL python client, imported lazily: (SigilMCP, compose_with_stdlib).

    Lazy on purpose. These used to be module-scope imports in agent.py and
    tests/conftest.py behind a `sys.path.insert`, which meant `import agent` —
    and therefore COLLECTING ANY TEST AT ALL — required a SIGIL checkout, even
    for the large fraction of the suite that never forges anything. Deferring
    the import to the first forge is what lets the pure tests run on a machine
    with no toolchain.

    PROVENANCE: these come from SIGIL's `bench/src` — its benchmark harness,
    not a published API — so they move when its bench layout moves. Vendoring
    them into this repo is the fix (and a prerequisite for issue #17, which
    cannot add connection pooling to a client it does not own). This function
    is where that swap lands: it prefers a vendored `sigil_client` package and
    falls back to the checkout, so vendoring changes this function and nothing
    else."""
    try:
        from sigil_client.compose import compose_with_stdlib
        from sigil_client.mcp_client import SigilMCP
        return SigilMCP, compose_with_stdlib
    except ImportError:
        pass

    root = Path(os.environ.get("SIGIL_ROOT",
                               PI_ROOT.parent / "SIGIL")).expanduser().resolve()
    bench = root / "bench" / "src"
    if str(bench) not in sys.path:
        sys.path.insert(0, str(bench))
    try:
        from sigil_bench.compose import compose_with_stdlib
        from sigil_bench.mcp_client import SigilMCP
    except ImportError as e:
        raise ToolchainNotFound(
            f"the SIGIL python client is not importable from {bench} ({e}). "
            "It ships with a SIGIL checkout; set SIGIL_ROOT to one.") from e
    return SigilMCP, compose_with_stdlib
