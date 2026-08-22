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


# Env vars that point resolution AWAY from a source checkout. Named here so
# callers that care about the arrangement rather than the result — ci.sh, which
# builds the binary and therefore cannot require one to exist yet — ask a
# question about configuration instead of probing the filesystem.
OVERRIDE_ENV = ("PI_FORGE_BIN", "PI_TOOLCHAIN_DIR")


def configured_override():
    """The name of the env var steering resolution away from a checkout, or
    None when nothing does.

    Exists because `resolve()` answers a DIFFERENT question than some callers
    are asking. resolve() means "give me a usable toolchain", which requires a
    built binary. ci.sh needs "am I in source mode?" BEFORE the cargo build
    that produces that binary — asking resolve() there fails on a clean
    checkout where nothing is built yet, which is the normal state of CI."""
    for name in OVERRIDE_ENV:
        if os.environ.get(name):
            return name
    return None


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
    if not forge.exists():
        # AN EXPLICIT PATH IS AN ASSERTION, NOT A SEARCH LOCATION. That is the
        # difference between this probe and the two below: a release directory
        # or a checkout that turns out to be empty is a place we LOOKED, so
        # falling through to the next one is right. PI_FORGE_BIN is a claim
        # about where the toolchain is, and quietly running a different one
        # because the claim was a typo is the exact failure the missing-stdlib
        # check above refuses to allow.
        raise ToolchainNotFound(
            f"PI_FORGE_BIN points at {forge}, which does not exist. Falling "
            f"back to a checkout would silently run a different toolchain "
            f"than the one named. Fix the path, or unset PI_FORGE_BIN.")
    return Toolchain(forge, serve, Path(stdlib).expanduser().resolve(),
                     "explicit (PI_FORGE_BIN)"), [forge]


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

    PROVENANCE: VENDORED. `runtime_client.ProductionSigilMCP` and
    `sigil_compose.compose_with_stdlib` are this repo's own copies of the two
    helpers that used to come from SIGIL's `bench/src` harness — a benchmark,
    not a published API, and one that moved whenever its bench layout moved.
    The vendored client differs from the bench one in exactly the ways a host
    that is not a benchmark must: it strips the benchmark-only
    SIGIL_ALLOW_UNVERIFIED_CERT override from the compiler's environment even
    when the operator's shell carries it, bounds every protocol response with
    a deadline that kills a wedged compiler, and exposes only the protocol
    surface sigil-pi uses. tests/test_sigil_compose.py pins the composer
    byte-identical to the pinned bench composer, and ci.sh step 2 regenerates
    chat_turn.sigil through it. This stays the single hand-out point so a
    pooled client (issue #17) lands as one function's change."""
    from runtime_client import ProductionSigilMCP
    from sigil_compose import compose_with_stdlib
    return ProductionSigilMCP, compose_with_stdlib
