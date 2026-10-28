"""End-to-end test against a real ansible-core (skipped if it is not installed)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ansible-galaxy") is None,
                                reason="ansible-core not installed")


def sh(*cmd, env, check=True, cwd=None):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=cwd, timeout=120)
    if check and r.returncode:
        raise AssertionError(f"{cmd} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def build(src, out, name, version, deps, env, marker):
    ns, coll = name.split(".")
    sh("ansible-galaxy", "collection", "init", name, "--init-path", str(src / version), env=env)
    path = src / version / ns / coll
    galaxy = (path / "galaxy.yml").read_text()
    galaxy = galaxy.replace("version: 1.0.0", f"version: {version}")
    galaxy = galaxy.replace("dependencies: {}", f"dependencies: {json.dumps(deps)}")
    (path / "galaxy.yml").write_text(galaxy)
    (path / "plugins" / "filter").mkdir(parents=True, exist_ok=True)
    (path / "plugins" / "filter" / "where.py").write_text(
        "class FilterModule:\n    def filters(self):\n"
        f"        return {{'where': lambda x: {marker!r}}}\n")
    sh("ansible-galaxy", "collection", "build", "--output-path", str(out), env=env, cwd=path)
    return out / f"{ns}-{coll}-{version}.tar.gz"


@pytest.fixture
def env(tmp_path):
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    e = dict(os.environ, HOME=str(tmp_path), ANSIBLE_COLLECTIONS_PATH=f"{p1}:{p2}",
             ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false", ANSIBLE_LOCALHOST_WARNING="false")
    src, out = tmp_path / "src", tmp_path / "dist"
    base1 = build(src, out, "demo.base", "1.0.0", {}, e, "base 1 in p1")
    base2 = build(src, out, "demo.base", "2.0.0", {}, e, "base 2 in p2")
    app = build(src, out, "demo.app", "1.0.0", {"demo.base": ">=1.0.0"}, e, "app")
    sh("ansible-galaxy", "collection", "install", "--offline", "-p", str(p1), str(app), str(base1),
       env=e)
    sh("ansible-galaxy", "collection", "install", "--offline", "-p", str(p2), str(base2), env=e)
    e["P1"], e["P2"], e["APP"] = str(p1), str(p2), str(app)
    return e


def gu(env, *args, check=True):
    return sh(sys.executable, "-m", "galaxy_uninstall", *args, env=env, check=check)


def loaded(env):
    r = sh("ansible", "localhost", "-m", "debug", "-a", "msg={{ 1 | demo.base.where }}", env=env)
    return r.stdout


def installed(env):
    data = json.loads(sh("ansible-galaxy", "collection", "list", "--format", "json", env=env).stdout)
    return sorted((os.path.basename(os.path.dirname(root)), name, meta["version"])
                  for root, colls in data.items() for name, meta in colls.items())


def test_list_matches_what_ansible_loads(env):
    out = gu(env, "--list").stdout
    assert "base 1 in p1" in loaded(env)
    line1 = next(l for l in out.splitlines() if "demo.base" in l and "1.0.0" in l)
    line2 = next(l for l in out.splitlines() if "demo.base" in l and "2.0.0" in l)
    assert "[active]" in line1 and "shadowed by" in line2


def test_remove_one_copy_then_ansible_loads_the_next(env):
    r = gu(env, "demo.base", "--all", "-y", check=False)
    assert r.returncode == 1 and "depends on demo.base" in r.stderr
    assert len(installed(env)) == 3

    r = gu(env, "demo.base", "--path", env["P1"], "-y")
    assert "Afterwards Ansible loads demo.base 2.0.0" in r.stdout
    assert "base 2 in p2" in loaded(env)
    assert installed(env) == [("p1", "demo.app", "1.0.0"), ("p2", "demo.base", "2.0.0")]


def test_deps_and_reinstall(env):
    r = gu(env, "demo.app", "--deps", "-n")
    assert "demo.base 1.0.0" in r.stdout and len(installed(env)) == 3
    gu(env, "demo.app", "--deps", "-y")
    assert installed(env) == [("p2", "demo.base", "2.0.0")]
    assert not os.path.exists(os.path.join(env["P1"], "ansible_collections", "demo"))
    sh("ansible-galaxy", "collection", "install", "--offline", "-p", env["P1"], env["APP"],
       env=env)
    assert ("p1", "demo.app", "1.0.0") in installed(env)


def test_shadowed(env):
    gu(env, "--shadowed", "-y")
    assert installed(env) == [("p1", "demo.app", "1.0.0"), ("p1", "demo.base", "1.0.0")]
    assert "base 1 in p1" in loaded(env)
