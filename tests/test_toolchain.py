"""The toolchain seam (issue #15).

sigil-pi is forged by a binary built from SIGIL. HOW that binary is obtained —
a source checkout, an installed release, an explicit path — is a deployment
detail, and the point of toolchain.py is that it stays one. These tests pin the
resolution order, the two pin modes, and the regression that motivated the
module: a module-scope import of the SIGIL checkout, which made `import agent`
(and collecting ANY test) impossible without a clone of a private repo.
"""
import re

import pytest

import toolchain
from conftest import PI_ROOT


def _layout(root, *, forge=True, stdlib=True, kind="release"):
    """A fake toolchain on disk. Contents are irrelevant — resolution is about
    shape, and using real binaries here would make these tests need the very
    thing they exist to make optional."""
    if kind == "release":
        binary = root / "bin" / "sigil-mcp"
    else:
        binary = root / "target" / "release" / "sigil-mcp"
    if forge:
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"not really a compiler")
    if stdlib:
        (root / "stdlib").mkdir(parents=True, exist_ok=True)
    return binary


# ── resolution order ────────────────────────────────────────────────────


def test_source_checkout_resolves(tmp_path, monkeypatch):
    """Path 3 — today's arrangement, and the developer path forever."""
    _layout(tmp_path / "src", kind="source")
    monkeypatch.delenv("PI_FORGE_BIN", raising=False)
    monkeypatch.delenv("PI_TOOLCHAIN_DIR", raising=False)
    monkeypatch.setenv("SIGIL_ROOT", str(tmp_path / "src"))
    tc = toolchain.resolve()
    assert tc.origin.startswith("source checkout")
    assert tc.stdlib_repo == (tmp_path / "src").resolve()


def test_release_beats_source(tmp_path, monkeypatch):
    """An installed toolchain wins over a checkout that happens to be there —
    otherwise a stale sibling clone silently overrides what was installed."""
    _layout(tmp_path / "src", kind="source")
    _layout(tmp_path / "rel")
    monkeypatch.delenv("PI_FORGE_BIN", raising=False)
    monkeypatch.setenv("SIGIL_ROOT", str(tmp_path / "src"))
    monkeypatch.setenv("PI_TOOLCHAIN_DIR", str(tmp_path / "rel"))
    assert toolchain.resolve().origin.startswith("release")


def test_explicit_beats_everything(tmp_path, monkeypatch):
    _layout(tmp_path / "src", kind="source")
    _layout(tmp_path / "rel")
    forge = tmp_path / "x" / "sigil-mcp"
    forge.parent.mkdir(parents=True)
    forge.write_bytes(b"x")
    monkeypatch.setenv("SIGIL_ROOT", str(tmp_path / "src"))
    monkeypatch.setenv("PI_TOOLCHAIN_DIR", str(tmp_path / "rel"))
    monkeypatch.setenv("PI_FORGE_BIN", str(forge))
    monkeypatch.setenv("PI_STDLIB_DIR", str(tmp_path / "rel"))
    tc = toolchain.resolve()
    assert tc.origin.startswith("explicit")
    assert tc.forge_bin == forge
    # serve defaults to a sibling of the forge binary rather than being
    # required — they ship together, and demanding both is friction with no
    # safety payoff.
    assert tc.serve_bin == forge.parent / "sigil-serve"


def test_a_release_without_a_stdlib_is_not_a_toolchain(tmp_path, monkeypatch):
    """THE failure this ordering exists to prevent. The agent composes guests
    against the stdlib on every turn, so a release carrying only the binary
    would import, serve, and die on the first request. Falling through to a
    checkout that HAS a stdlib is better than resolving to something that
    cannot work."""
    _layout(tmp_path / "src", kind="source")
    _layout(tmp_path / "rel", stdlib=False)
    monkeypatch.delenv("PI_FORGE_BIN", raising=False)
    monkeypatch.setenv("SIGIL_ROOT", str(tmp_path / "src"))
    monkeypatch.setenv("PI_TOOLCHAIN_DIR", str(tmp_path / "rel"))
    assert toolchain.resolve().origin.startswith("source checkout")


def test_explicit_without_a_stdlib_is_loud(tmp_path, monkeypatch):
    """Explicit-but-incomplete is an operator typo. Falling back silently
    would run a DIFFERENT toolchain than the one they named — the failure
    mode SIGIL_REV exists to make impossible."""
    forge = tmp_path / "x" / "sigil-mcp"
    forge.parent.mkdir(parents=True)
    forge.write_bytes(b"x")
    monkeypatch.setenv("PI_FORGE_BIN", str(forge))
    monkeypatch.delenv("PI_STDLIB_DIR", raising=False)
    with pytest.raises(toolchain.ToolchainNotFound, match="PI_STDLIB_DIR"):
        toolchain.resolve()


def test_not_found_names_every_path_tried(tmp_path, monkeypatch):
    """'toolchain not found' without the search list is a bug report nobody
    can act on."""
    monkeypatch.delenv("PI_FORGE_BIN", raising=False)
    monkeypatch.setenv("PI_TOOLCHAIN_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("SIGIL_ROOT", str(tmp_path / "also-nope"))
    with pytest.raises(toolchain.ToolchainNotFound) as e:
        toolchain.resolve()
    assert "nope" in str(e.value) and "also-nope" in str(e.value)
    assert toolchain.resolve(require=False) is None


# ── the binary pin (the second mode) ─────────────────────────────────────


def test_platform_key_normalises_spellings():
    """amd64/x86_64 and arm64/aarch64 are the same machines under two names.
    Without normalising, SIGIL_REV would need an alias per OS spelling."""
    assert re.fullmatch(r"[a-z0-9]+_(x86_64|arm64|[a-z0-9_]+)",
                        toolchain.platform_key())


def test_binary_pin_catches_a_mismatch(tmp_path):
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(b"the real one")
    key = f"sha256_{toolchain.platform_key()}"
    good = toolchain.sha256_file(binary)
    assert toolchain.verify_binary(binary, {key: good}) is None
    binary.write_bytes(b"a different build entirely")
    problem = toolchain.verify_binary(binary, {key: good})
    assert problem and good in problem, "the problem must show both digests"


def test_no_pin_for_this_platform_is_not_a_failure(tmp_path):
    """There are no published binaries yet. Treating 'nothing to check' as
    tampering would make the first release unrunnable."""
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(b"x")
    assert toolchain.verify_binary(binary, {}) is None


def test_sigil_rev_still_parses_with_the_new_keys():
    rev = toolchain.read_rev()
    assert re.fullmatch(r"[0-9a-f]{40}", rev["ref"])
    for area in ("crates", "stdlib"):
        assert re.fullmatch(r"[0-9a-f]{40}", rev[area])
    for key, value in rev.items():
        if key.startswith("sha256_"):
            assert re.fullmatch(r"[0-9a-f]{64}", value), \
                f"{key} must be a full sha256, got {value!r}"


# ── the regression this module exists to prevent ─────────────────────────


def test_no_module_scope_import_of_the_sigil_checkout():
    """THE bug class. A `sys.path.insert` into a SIGIL checkout at module
    scope, followed by `from sigil_bench...`, means importing the module
    requires a clone of a private repo — and in conftest.py it meant the whole
    suite could not even be COLLECTED without one, including the ~120 tests
    that never forge anything. Lazy resolution is the fix; this pins it.

    toolchain.py itself is exempt: doing it lazily, in one place, is the
    entire point of the module."""
    for name in ("agent.py", "make_chat_turn.py", "tests/conftest.py"):
        src = (PI_ROOT / name).read_text()
        top_level = [ln for ln in src.splitlines()
                     if ln.startswith(("import ", "from ", "sys.path.insert"))]
        joined = "\n".join(top_level)
        assert "sigil_bench" not in joined, (
            f"{name} imports sigil_bench at module scope — that reinstates the "
            f"private-repo dependency the toolchain seam removed")
        assert "bench" not in joined, (
            f"{name} puts a SIGIL checkout on sys.path at module scope; route "
            f"it through toolchain.client() instead")


def test_configured_override_reports_the_arrangement_not_the_result(monkeypatch):
    for name in toolchain.OVERRIDE_ENV:
        monkeypatch.delenv(name, raising=False)
    assert toolchain.configured_override() is None
    monkeypatch.setenv("PI_TOOLCHAIN_DIR", "/nonexistent/on/purpose")
    # Answers from the ENVIRONMENT, deliberately: the whole point is to be
    # callable before any binary exists.
    assert toolchain.configured_override() == "PI_TOOLCHAIN_DIR"


def test_ci_sh_does_not_require_a_built_binary_before_it_builds_one():
    """Regression, found by CI on the first run of this seam.

    ci.sh step 1 verifies the pin and then REBUILDS the forge binaries from
    the tree it just proved (issue #6). A check in that step which requires a
    binary to already exist is therefore self-defeating: it fails on every
    clean checkout — including CI's, where nothing is built yet — before the
    build that would satisfy it. `toolchain.resolve()` is exactly such a
    check, because it means 'give me a usable toolchain'. Step 1 must ask
    about CONFIGURATION (configured_override) instead."""
    src = (PI_ROOT / "ci.sh").read_text()
    step1 = src.split("── 2/4", 1)[0]
    # Comments stripped before matching: this step's own comment explains why
    # resolve() is wrong here and would otherwise trip the check it documents.
    # Rewording the prose would work once; stripping keeps it true for the
    # next person who explains the same thing.
    code = "\n".join(ln for ln in step1.splitlines()
                     if not ln.strip().startswith("#"))
    assert "configured_override" in code, \
        "ci.sh step 1 lost its source-mode check"
    assert "toolchain.resolve(" not in code, (
        "ci.sh step 1 must not call toolchain.resolve() — it requires a built "
        "binary, and this is the step that builds it")


def test_the_forge_ci_job_requires_a_toolchain():
    """The forge job must set PI_REQUIRE_TOOLCHAIN, or its 221 forge tests
    would SKIP on a machine without SIGIL and the job would report green
    having verified nothing — the same lie ci.yml's header already guards
    against one level up, at the job gate."""
    ci = (PI_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    forge_job = ci.split("\n  forge:", 1)
    assert len(forge_job) == 2, "ci.yml lost its forge job"
    assert toolchain.REQUIRE_ENV in forge_job[1], (
        f"the forge job must set {toolchain.REQUIRE_ENV}=1 so a missing "
        f"toolchain fails it instead of silently emptying it")
