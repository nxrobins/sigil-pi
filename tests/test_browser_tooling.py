"""Mandatory client units and reproducible browser-test tooling, not app policy."""
import json
import os
import re
import shutil
import subprocess

from conftest import PI_ROOT


def node():
    executable = shutil.which("node")
    assert executable is not None, "browser development checks require Node.js; see .node-version"
    return executable


def test_all_browser_client_unit_files_execute():
    files = sorted((PI_ROOT / "tests").glob("*.test.mjs"))
    assert {"api.test.mjs", "controller.test.mjs"} <= {path.name for path in files}
    result = subprocess.run([node(), "--test", *map(str, files)], cwd=PI_ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_browser_modules_parse():
    files = sorted([*(PI_ROOT / "web").glob("*.mjs"), *(PI_ROOT / "tests").glob("*.mjs"),
                    PI_ROOT / "scripts/browser_runtime.mjs"])
    assert {"app.mjs", "api.mjs", "browser_flow.mjs", "browser_boundaries.mjs",
            "browser_accounting.mjs", "browser_outage.mjs", "browser_connections.mjs"} <= {path.name for path in files}
    for path in files:
        result = subprocess.run([node(), "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr


def test_browser_test_packages_are_exactly_locked_and_not_production_dependencies():
    manifest = json.loads((PI_ROOT / "package.json").read_text())
    lock = json.loads((PI_ROOT / "package-lock.json").read_text())
    assert manifest["private"] is True and not manifest.get("dependencies")
    assert lock["lockfileVersion"] == 3
    version = manifest["devDependencies"]["playwright"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert lock["packages"][""]["devDependencies"] == {"playwright": version}
    for name in ["playwright", "playwright-core"]:
        package = lock["packages"]["node_modules/" + name]
        assert package["version"] == version and package["dev"] is True
        assert package["resolved"] == f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
        assert re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", package["integrity"])
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", (PI_ROOT / ".node-version").read_text())


def test_both_ci_jobs_and_release_install_locked_browser_prerequisites():
    ci = (PI_ROOT / ".github/workflows/ci.yml").read_text()
    release = (PI_ROOT / ".github/workflows/release.yml").read_text()
    standalone, forge = ci.split("\n  forge:", 1)
    for body in [standalone, forge, release]:
        assert "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38" in body
        assert "node-version-file:" in body and ".node-version" in body
        assert "npm ci --ignore-scripts --no-audit --no-fund" in body
    for body in [forge, release]:
        install = "npx --no-install playwright install --with-deps chromium"
        assert install in body
        assert body.index(install) < body.rindex("./ci.sh")
        assert 'PI_REQUIRE_TOOLCHAIN: "1"' in body


def runtime_check(override):
    return subprocess.run([node(), str(PI_ROOT / "scripts/browser_runtime.mjs")], cwd=PI_ROOT,
        env={**os.environ, "PI_PLAYWRIGHT_DIR": str(override)}, capture_output=True, text=True, timeout=30)


def test_browser_runtime_rejects_relative_override():
    result = runtime_check("relative/package")
    assert result.returncode == 1 and result.stdout == ""
    assert "must be an absolute" in json.loads(result.stderr)["detail"]


def test_browser_runtime_rejects_missing_package(tmp_path):
    result = runtime_check(tmp_path / "absent")
    assert result.returncode == 1 and result.stdout == ""
    assert json.loads(result.stderr)["error"] == "browser_runtime_unavailable"


def test_browser_runtime_checks_version_before_loading_a_wrong_module(tmp_path):
    fake = tmp_path / "wrong-package"
    fake.mkdir()
    (fake / "package.json").write_text(json.dumps({"version": "0.0.0", "main": "index.js"}))
    (fake / "index.js").write_text("throw new Error('must-not-load-the-wrong-module');\n")
    result = runtime_check(fake)
    assert result.returncode == 1 and result.stdout == ""
    detail = json.loads(result.stderr)["detail"]
    assert "does not match package.json" in detail and "must-not-load" not in detail
