"""F-20: the shipped documents say what the code does.

Audit 2026-11-10 found documents that contradicted the code and each other:
settings the product reads that the operations guide never names, a security
policy naming an old version, a threat model and a blocker list that still
said no release had verified provenance, an API sentence saying a turn
deadline drops readiness, a guide telling operators to alert on a counter the
alert policy does not watch, a tool comment naming a grant its manifest does
not have, a changelog claim that the bench client was imported nowhere while
`pip install` installed two modules that import it, and a README calling audit
gaps "structurally impossible". Each test below holds one of those classes to
the code, so the next change to the code fails here instead of in a reader's
hands.
"""

import ast
import json
import re
import tomllib
from datetime import date
from pathlib import Path

import product_main
import pytest
from conftest import PI_ROOT

OPERATIONS = PI_ROOT / "docs" / "operations.md"
# The product entry point and the service it runs: what an operator configures.
PRODUCT_SOURCES = ("product_main.py", "product_service.py")
_ENV_READERS = {"_positive_env_int", "_env_int", "_required_env", "get", "getenv"}
_SETTING = re.compile(r"[A-Z][A-Z0-9_]*")
# The parity-checked documents an operator is handed with a release.
SHIPPED_CONTRACTS = ("docs/api.md", "docs/operations.md", "docs/runbooks.md",
                     "docs/security/threat-model.md")


# ── the settings the product reads, and what the guide says of them ────────


def _reader_name(call):
    function = call.func
    return getattr(function, "id", None) or getattr(function, "attr", None)


def _env_call(node):
    """The variable an environment read names, or None."""
    if (isinstance(node, ast.Call) and node.args and _reader_name(node) in _ENV_READERS
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _SETTING.fullmatch(node.args[0].value)):
        return node.args[0].value
    return None


def _settings(source):
    """{name: default expression or None} for every variable `source` reads,
    and the prefixes it scans the environment for."""
    tree = ast.parse((PI_ROOT / source).read_text())
    read, prefixes = {}, set()
    for node in ast.walk(tree):
        name = _env_call(node)
        if name is not None:
            default = node.args[1] if len(node.args) > 1 else None
            read.setdefault(name, default)
            if default is not None:
                read[name] = default
        elif (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
              and _env_call(node.values[0]) and len(node.values[0].args) == 1):
            # `os.environ.get("NAME") or DEFAULT`
            read[_env_call(node.values[0])] = node.values[1]
        elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
              and isinstance(node.slice.value, str)
              and "environ" in ast.unparse(node.value)):
            read.setdefault(node.slice.value, None)
        elif (isinstance(node, ast.Call) and _reader_name(node) == "startswith"
              and "name" in ast.unparse(node.func.value) and node.args
              and isinstance(node.args[0], ast.Constant)
              and str(node.args[0].value).startswith("PI_")):
            prefixes.add(node.args[0].value)
    return tree, read, prefixes


def _setting_locals(tree):
    """{local variable: the setting it was read from}, so a default that is
    another setting's value is stated as that setting."""
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and _env_call(node.value)):
            found[node.targets[0].id] = _env_call(node.value)
    return found


def _expected_default(expression, local_settings):
    """The default as the guide must state it: a set of acceptable spellings,
    or None when there is no default to state."""
    if expression is None:
        return None
    if isinstance(expression, ast.Name) and expression.id in local_settings:
        return {local_settings[expression.id]}
    value = eval(compile(ast.Expression(expression), "<default>", "eval"),
                 vars(product_main))
    if value is None or value == "":
        return None
    if isinstance(value, Path):
        return {str(value.relative_to(product_main.PI_ROOT))}
    if isinstance(value, int) and not isinstance(value, bool):
        spellings = {str(value), f"{value:,}"}
        for unit, size in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
            if value % size == 0:
                spellings.add(f"{value // size} {unit}")
        return spellings
    return {str(value)}


def _items(markdown):
    """The guide's bullets and paragraphs, each as one string."""
    items = []
    for block in re.split(r"\n\s*\n", markdown):
        items.extend(part for part in re.split(r"\n(?=- )", block) if part.strip())
    return items


def _stated_defaults(markdown, name):
    """Every value the guide states as `name`'s default: the backticked value
    after the word "default" that follows the name in the same bullet or
    paragraph, with no other setting named in between."""
    stated = set()
    for item in _items(markdown):
        for mention in re.finditer(rf"`{re.escape(name)}`", item):
            rest = item[mention.end():]
            default = re.search(r"\bdefaults?\b(?: to| is| and maximum)?\s*`([^`]+)`", rest)
            if default and not re.search(r"`(?:PI|ANTHROPIC)_[A-Z0-9_]+`",
                                         rest[:default.start()]):
                stated.add(default.group(1))
    return stated


def test_every_setting_the_product_reads_is_in_the_operations_guide_with_its_default():
    """The class guard for the guide's settings. product_main read
    `PI_MAX_TOKENS`, `PI_MODEL`, `PI_ENDPOINT`, `PI_SYSTEM`, `PI_LLM_RETRIES`
    and `PI_SECRET_*` and the guide named none of them; settings added by the
    remediation (the compiler pool, the model HTTP timeout, the turn ceiling,
    the request deadline, the connection cap, the tenant secrets) were added
    to the code first. A setting the product reads is part of the operator's
    contract, and so is the value it takes when nobody sets it."""
    guide = OPERATIONS.read_text()
    missing, undefaulted, with_default = [], [], 0
    for source in PRODUCT_SOURCES:
        tree, read, prefixes = _settings(source)
        local_settings = _setting_locals(tree)
        for prefix in sorted(prefixes):
            if f"`{prefix}" not in guide:
                missing.append(f"{prefix}*")
        for name, expression in sorted(read.items()):
            expected = _expected_default(expression, local_settings)
            with_default += expected is not None
            if f"`{name}`" not in guide:
                missing.append(name)
                continue
            stated = _stated_defaults(guide, name)
            if expected is not None and not stated & expected:
                undefaulted.append(
                    f"{name} (code: {sorted(expected)}, guide: {sorted(stated) or 'nothing'})")
    _, product_main_reads, _ = _settings("product_main.py")
    assert {"PI_TURN_DEADLINE_SECONDS", "ANTHROPIC_API_KEY", "PI_THINKING"} <= set(
        product_main_reads) and len(product_main_reads) >= 40 and with_default >= 30, (
        "the scan found too little: it would pass vacuously")
    assert not missing, (
        f"the product reads {missing}, which docs/operations.md does not name "
        f"(audit F-20)")
    assert not undefaulted, (
        f"docs/operations.md does not state the default the code uses for: "
        f"{'; '.join(undefaulted)} (audit F-20)")


def test_the_settings_guard_reads_defaults_the_way_the_code_does():
    """Anti-vacuity for the guard above: each shape of read it relies on."""
    tree, read, prefixes = _settings("product_main.py")
    local_settings = _setting_locals(tree)
    assert _expected_default(read["PI_MAX_SCHEDULE_BYTES_PER_TENANT"],
                             local_settings) >= {"8 MiB", "8388608"}
    assert _expected_default(read["PI_THINKING"], local_settings) == {"adaptive"}
    assert _expected_default(read["PI_MAX_TOKENS_PER_TURN"], local_settings) == {
        "PI_TOKEN_RESERVATION_PER_TURN"}
    assert _expected_default(read["PI_STATE"], local_settings) == {".pi-state"}
    assert _expected_default(read["ANTHROPIC_API_KEY"], local_settings) is None
    assert prefixes == {"PI_SECRET_"}
    sample = ("- `PI_A` (default `1`) and `PI_B` defaults to `2`.\n"
              "- `PI_C` bounds it. Its default is `PI_A`.\n\n`PI_D` is `3`.")
    assert _stated_defaults(sample, "PI_A") == {"1"}
    assert _stated_defaults(sample, "PI_B") == {"2"}
    assert _stated_defaults(sample, "PI_C") == {"PI_A"}
    assert _stated_defaults(sample, "PI_D") == set()


# ── release facts the documents restate ────────────────────────────────────


def test_the_security_policy_names_the_current_release():
    """SECURITY.md ships in the bundle and said `0.3.0` was the current
    internal-alpha release while VERSION said 0.4.0."""
    version = (PI_ROOT / "VERSION").read_text().strip()
    policy = (PI_ROOT / "SECURITY.md").read_text()
    named = set(re.findall(r"\b\d+\.\d+\.\d+\b", policy))
    assert version in named and named <= {version}, (
        f"SECURITY.md names {sorted(named)}; VERSION is {version} (audit F-20)")


def _release_records():
    """(date verified, path) of every release whose attestations a record in
    docs/evidence says were verified."""
    records = []
    for path in sorted((PI_ROOT / "docs" / "evidence").glob("release-*.md")):
        text = path.read_text()
        verified = re.search(r"Verified (\d{4}-\d{2}-\d{2})", text)
        if "cryptographically verified" in text and verified:
            records.append((date.fromisoformat(verified[1]), path.name))
    return records


def test_no_document_says_no_release_has_verified_provenance_once_one_has():
    """The threat model (last updated 2026-08-22) and the operations guide's
    blocker list still said no protected tag run had produced verified
    provenance after v0.4.0's attestations were verified on 2026-09-07. The
    threat model is the document a reviewer is handed, so it must be at least
    as new as the newest verified release it describes."""
    records = _release_records()
    assert records, "no verified release record found: this would pass vacuously"
    newest, record = max(records)
    stale_claims = ("no protected tag run has yet produced",
                    "successful signed tag evidence")
    for name in ("docs/security/threat-model.md", "docs/operations.md"):
        text = " ".join((PI_ROOT / name).read_text().split())
        for claim in stale_claims:
            assert claim not in text, (
                f"{name} still says {claim!r}, but {record} records verified "
                f"provenance (audit F-20)")
    model = (PI_ROOT / "docs" / "security" / "threat-model.md").read_text()
    updated = re.search(r"Last updated (\d{4}-\d{2}-\d{2})", model)
    assert updated, "the threat model lost its 'Last updated' date"
    # These documents are byte-for-byte identical to the frozen release's copy.
    # Later verification records do not revise the historical claims it cites.
    cited = set(re.findall(r"docs/evidence/(release-[^\s/`\)]+\.md)", model))
    assert cited, "the threat model cites no release verification record"
    verified = {name: verified_date for verified_date, name in records}
    assert not cited - verified.keys(), (
        f"the threat model cites records without dated verified provenance: "
        f"{sorted(cited - verified.keys())}")
    newest, record = max((verified[name], name) for name in cited)
    assert date.fromisoformat(updated[1]) >= newest, (
        f"the threat model was last updated {updated[1]}, before {record} "
        f"({newest}) recorded a verified release (audit F-20)")


def _provenance_fixture(tmp_path, monkeypatch, *, model=None):
    """Exercise the actual guard against small, independent evidence records."""
    monkeypatch.setitem(globals(), "PI_ROOT", tmp_path)
    evidence = tmp_path / "docs" / "evidence"
    evidence.mkdir(parents=True)
    security = tmp_path / "docs" / "security"
    security.mkdir()
    (tmp_path / "docs" / "operations.md").write_text("Provenance is verified.\n")
    (security / "threat-model.md").write_text(model if model is not None else (
        "Last updated 2026-11-15\n"
        "Verified provenance: `docs/evidence/release-0.4.0.md`.\n"))
    (evidence / "release-0.4.0.md").write_text(
        "Verified 2026-09-07: cryptographically verified.\n")
    (evidence / "release-0.5.0.md").write_text(
        "Verified 2026-11-18: cryptographically verified.\n")
    return evidence


def test_new_evidence_does_not_require_rewriting_a_frozen_threat_model(tmp_path, monkeypatch):
    _provenance_fixture(tmp_path, monkeypatch)
    test_no_document_says_no_release_has_verified_provenance_once_one_has()


@pytest.mark.parametrize("model, expected", [
    ("Last updated 2026-11-15\n`docs/evidence/release-0.5.0.md`", "before release-0.5.0.md"),
    ("Last updated 2026-11-18\n", "cites no release verification record"),
    ("Last updated 2026-11-18\n`docs/evidence/release-0.4.0.md` "
     "`docs/evidence/release-0.6.0.md`", "release-0.6.0.md"),
    ("`docs/evidence/release-0.4.0.md`", "lost its 'Last updated' date"),
])
def test_provenance_guard_rejects_missing_or_stale_cited_evidence(
        tmp_path, monkeypatch, model, expected):
    _provenance_fixture(tmp_path, monkeypatch, model=model)
    with pytest.raises(AssertionError, match=re.escape(expected)):
        test_no_document_says_no_release_has_verified_provenance_once_one_has()


@pytest.mark.parametrize("contents", ["Undated: cryptographically verified.",
                                      "Verified 2026-09-07: not checked."])
def test_provenance_guard_rejects_malformed_cited_records(tmp_path, monkeypatch, contents):
    evidence = _provenance_fixture(tmp_path, monkeypatch)
    (evidence / "release-0.4.0.md").write_text(contents)
    with pytest.raises(AssertionError, match="release-0.4.0.md"):
        test_no_document_says_no_release_has_verified_provenance_once_one_has()


@pytest.mark.parametrize("document", ["docs/operations.md", "docs/security/threat-model.md"])
@pytest.mark.parametrize("claim", ["no protected tag run has yet produced",
                                    "successful signed tag evidence"])
def test_provenance_guard_still_rejects_blanket_stale_claims(
        tmp_path, monkeypatch, document, claim):
    _provenance_fixture(tmp_path, monkeypatch)
    path = tmp_path / document
    path.write_text(path.read_text() + "\n" + claim)
    with pytest.raises(AssertionError, match="still says"):
        test_no_document_says_no_release_has_verified_provenance_once_one_has()


def test_provenance_guard_requires_a_verified_record(tmp_path, monkeypatch):
    evidence = _provenance_fixture(tmp_path, monkeypatch)
    for path in evidence.glob("release-*.md"):
        path.unlink()
    with pytest.raises(AssertionError, match="no verified release record found"):
        test_no_document_says_no_release_has_verified_provenance_once_one_has()


# ── claims about behaviour ─────────────────────────────────────────────────


def _sentences(text):
    return re.split(r"(?<=[.;:])\s+", " ".join(text.split()))


def test_no_shipped_contract_says_a_turn_deadline_drops_readiness():
    """docs/api.md said a deadline expiry "drops readiness until the compiler
    process is restarted". The compiler is retired and replaced, and
    readiness is unaffected (runtime_client.SupervisedRuntime), as the same
    file said twenty lines later."""
    wrong = []
    for name in SHIPPED_CONTRACTS:
        for sentence in _sentences((PI_ROOT / name).read_text()):
            if "deadline" in sentence and re.search(
                    r"drops? readiness|readiness (?:fails|drops) until", sentence):
                wrong.append(f"{name}: {sentence}")
    assert not wrong, f"a document says a turn deadline drops readiness: {wrong}"
    api = " ".join((PI_ROOT / "docs" / "api.md").read_text().split())
    assert "replaced" in api and "readiness is unaffected" in api, (
        "docs/api.md no longer says what happens to readiness at a deadline")


def test_every_metric_the_operations_guide_says_to_alert_on_has_an_alert():
    """The guide said "alert on its rate" of
    `sigil_pi_runtime_unhealthy_replacements_total`, and the alert policy had
    no alert that reads it."""
    policy = json.loads((PI_ROOT / "config" / "alert-policy.json").read_text())
    watched = {value for alert in policy["alerts"]
               for key, value in alert["condition"].items() if key.endswith("path")}
    told = set()
    text = " ".join(OPERATIONS.read_text().split())
    for sentence in re.split(r"(?<=\.)\s+", text):
        if re.search(r"\balert on\b", sentence):
            told.update(re.findall(r"`sigil_pi_([a-z0-9_]+)`", sentence))
    assert told, "the guide names no metric to alert on: this would pass vacuously"
    unwatched = sorted(name for name in told if name not in watched)
    assert not unwatched, (
        f"docs/operations.md says to alert on {unwatched}, which no alert in "
        f"config/alert-policy.json reads (audit F-20)")


def test_every_grant_a_tool_comment_names_is_in_its_manifest():
    """tools/gh_fetch.sigil said its manifest grants `secret: [{GITHUB_TOKEN}]`;
    the manifest grants `{SECRET:github}`. A tool's header is where a reviewer
    reads its capabilities, so it must name the grants the manifest gives."""
    manifest = json.loads((PI_ROOT / "tools" / "manifest.json").read_text())
    wrong, checked = [], 0
    for entry in manifest.values():
        if not isinstance(entry, dict) or "source" not in entry:
            continue
        source = (PI_ROOT / entry["source"]).read_text()
        for line in source.splitlines():
            if "MANIFEST:" not in line:
                continue
            for kind, values in re.findall(r"\b(fs_write|fs|net|secret)\s*:\s*\[([^\]]*)\]",
                                           line):
                granted = {value.strip("{}").lower()
                           for value in entry["grants"].get(kind, [])}
                for value in filter(None, (v.strip() for v in values.split(","))):
                    checked += 1
                    if value.strip("{}").lower() not in granted:
                        wrong.append(f"{entry['source']}: {kind}: {value}")
    assert checked >= 10, "too few MANIFEST comments parsed: this would pass vacuously"
    assert not wrong, (
        f"tool comments name grants their manifest entry does not give: {wrong} "
        f"(audit F-20)")


def test_no_installed_module_imports_the_bench_client():
    """The 0.1.0 changelog says SIGIL's bench client "is no longer imported
    anywhere at runtime", but `pip install` installed `chat` and `drive`, the
    milestone-1 drivers, which import it at module scope. Whatever the
    package installs must import without a SIGIL checkout."""
    project = tomllib.loads((PI_ROOT / "pyproject.toml").read_text())
    modules = project["tool"]["setuptools"]["py-modules"]
    assert "product_main" in modules and "agent" in modules
    # Assembled at runtime, as tests/test_guards.py assembles it, so that this
    # file does not trip that guard's search of the suite for bench imports.
    bench = "sigil_" + "ben" + "ch"
    offenders = set()
    for module in modules:
        tree = ast.parse((PI_ROOT / f"{module}.py").read_text())
        for node in tree.body:
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(name.split(".")[0] == bench for name in names):
                offenders.add(module)
    assert not offenders, (
        f"pyproject.toml installs {sorted(offenders)}, which import SIGIL's bench "
        f"client at module scope (audit F-20)")


def test_the_readme_does_not_overstate_what_the_audit_log_guarantees():
    """README milestone 13 said recording in `_forge` makes audit gaps
    "structurally impossible". A record can still fail to be written; what
    the product guarantees is that its log is strict, so a turn that cannot
    record its work fails and withholds it (audit F-04)."""
    readme = " ".join((PI_ROOT / "README.md").read_text().split())
    assert "structurally impossible" not in readme, (
        "README.md still says audit gaps are structurally impossible (audit F-20)")
    assert "strict=True" in (PI_ROOT / "product_main.py").read_text()
    assert "The product host runs its log strict." in readme, (
        "product_main.py runs its audit log strict and README.md does not say so")
