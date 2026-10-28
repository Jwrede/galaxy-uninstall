import json

import pytest

from galaxy_uninstall import cli, core


def install(root, name, version="1.0.0", deps=None, info=False):
    ns, coll = name.split(".")
    path = root / "ansible_collections" / ns / coll
    path.mkdir(parents=True)
    (path / "MANIFEST.json").write_text(json.dumps(
        {"collection_info": {"namespace": ns, "name": coll, "version": version,
                             "dependencies": deps or {}}}))
    if info:
        (root / "ansible_collections" / f"{name}-{version}.info").mkdir()
        (root / "ansible_collections" / f"{name}-{version}.info" / "GALAXY.yml").write_text("x")
    return path


def listing(*roots):
    """What 'ansible-galaxy collection list --format json' prints for these roots."""
    out = {}
    for root in roots:
        base = root / "ansible_collections"
        out[str(base)] = {}
        for manifest in sorted(base.glob("*/*/MANIFEST.json")):
            info = json.loads(manifest.read_text())["collection_info"]
            out[str(base)][f"{info['namespace']}.{info['name']}"] = {"version": info["version"]}
    return out


@pytest.fixture
def tree(tmp_path):
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    install(p1, "demo.app", deps={"demo.base": ">=1.0.0"})
    install(p1, "demo.base", info=True)
    install(p1, "demo.other")
    install(p2, "demo.base", "2.0.0")
    return p1, p2


def inv(tree, *extra):
    p1, p2 = tree
    return core.inventory([str(p1), str(p2)], listing(p1, p2, *extra))


def by(copies, name, version=None):
    return [c for c in copies if c.name == name and (version is None or c.version == version)]


def test_search_order_and_shadowing(tree):
    copies = inv(tree)
    act = core.active(copies)
    assert act["demo.base"].version == "1.0.0"
    assert [c.version for c in core.shadowed(copies)] == ["2.0.0"]


def test_config_order_decides_not_listing_order(tree):
    p1, p2 = tree
    copies = core.inventory([str(p2), str(p1)], listing(p1, p2))
    assert core.active(copies)["demo.base"].version == "2.0.0"


def test_configured_path_may_end_in_ansible_collections(tree):
    p1, p2 = tree
    copies = core.inventory([str(p1 / "ansible_collections"), str(p2)], listing(p1, p2))
    assert all(c.rank is not None for c in copies)


def test_site_packages_ranks_after_configured_and_unconfigured_is_off_path(tree, tmp_path):
    sp = tmp_path / "lib" / "site-packages"
    off = tmp_path / "elsewhere"
    install(sp, "demo.base", "3.0.0")
    install(off, "demo.base", "4.0.0")
    copies = inv(tree, sp, off)
    ranks = {c.version: c.rank for c in by(copies, "demo.base")}
    assert ranks["3.0.0"] == 2 and ranks["4.0.0"] is None
    assert core.active(copies)["demo.base"].version == "1.0.0"


def test_select_requires_choice_when_installed_twice(tree):
    copies = inv(tree)
    with pytest.raises(core.Error, match="installed 2 times"):
        core.select(copies, ["demo.base"], [], None, False)
    assert len(core.select(copies, ["demo.base"], [], None, True)) == 2
    assert core.select(copies, ["demo.base"], [], "2.0.0", False)[0].root.parent == tree[1]
    assert core.select(copies, ["demo.base"], [str(tree[0])], None, False)[0].version == "1.0.0"


def test_select_errors(tree):
    copies = inv(tree)
    with pytest.raises(core.Error, match="not installed"):
        core.select(copies, ["demo.nope"], [], None, False)
    with pytest.raises(core.Error, match="namespace.collection"):
        core.select(copies, ["base"], [], None, False)
    with pytest.raises(core.Error, match="no installed copy"):
        core.select(copies, ["demo.app"], [], "9.9.9", False)


def test_broken_only_when_no_copy_is_left(tree):
    copies = inv(tree)
    one = core.select(copies, ["demo.base"], [], "1.0.0", False)
    assert core.broken(copies, one) == []
    both = core.select(copies, ["demo.base"], [], None, True)
    assert [(c.name, d) for c, d in core.broken(copies, both)] == [("demo.app", "demo.base")]
    assert core.broken(copies, both + by(copies, "demo.app")) == []


def test_orphans_same_directory_only(tree):
    copies = inv(tree)
    plan = core.with_orphans(copies, by(copies, "demo.app"), [])
    assert sorted((c.name, c.version) for c in plan) == [("demo.app", "1.0.0"),
                                                          ("demo.base", "1.0.0")]
    assert core.with_orphans(copies, by(copies, "demo.app"), ["demo.base"]) == by(copies, "demo.app")


def test_orphans_kept_when_still_needed(tree, tmp_path):
    p1, p2 = tree
    install(p2, "demo.tool", deps={"demo.base": "*"})
    copies = inv(tree)
    plan = core.with_orphans(copies, by(copies, "demo.app"), [])
    assert [c.name for c in plan] == ["demo.app"]


def test_orphans_transitive(tmp_path):
    install(tmp_path, "a.top", deps={"a.mid": "*"})
    install(tmp_path, "a.mid", deps={"a.low": "*"})
    install(tmp_path, "a.low")
    copies = core.inventory([str(tmp_path)], listing(tmp_path))
    plan = core.with_orphans(copies, by(copies, "a.top"), [])
    assert sorted(c.name for c in plan) == ["a.low", "a.mid", "a.top"]


def test_pip_owned_and_checkout_detection(tmp_path):
    sp = tmp_path / "site-packages"
    install(sp, "community.general")
    install(sp, "demo.loose")
    dist = sp / "ansible-12.0.0.dist-info"
    dist.mkdir()
    (dist / "RECORD").write_text(
        "ansible_collections/community/general/MANIFEST.json,sha256=x,1\n"
        "ansible_collections/community/general/plugins/x.py,sha256=x,1\n")
    install(tmp_path / "dev", "my.coll")
    (tmp_path / "dev" / "ansible_collections" / "my" / "coll" / ".git").mkdir()
    copies = core.inventory([str(tmp_path / "dev")], listing(sp, tmp_path / "dev"))
    owners = {c.name: c.owner for c in copies}
    assert owners == {"community.general": "ansible", "demo.loose": None, "my.coll": None}
    with pytest.raises(core.Error, match="pip package 'ansible'"):
        core.blockers(copies, by(copies, "community.general"))
    assert core.blockers(copies, by(copies, "my.coll")) == by(copies, "my.coll")


def test_delete_removes_info_and_empty_namespace(tree):
    copies = inv(tree)
    base = by(copies, "demo.base", "1.0.0")[0]
    removed = core.delete(base)
    root = tree[0] / "ansible_collections"
    assert not (root / "demo" / "base").exists()
    assert not (root / "demo.base-1.0.0.info").exists()
    assert (root / "demo").is_dir()  # demo.app still there
    assert len(removed) == 2
    for c in by(copies, "demo.app") + by(copies, "demo.other"):
        core.delete(c)
    assert not (root / "demo").exists() and root.is_dir()


def test_delete_symlink_keeps_target(tmp_path):
    src = install(tmp_path / "src", "my.coll")
    root = tmp_path / "p" / "ansible_collections" / "my"
    root.mkdir(parents=True)
    (root / "coll").symlink_to(src)
    copies = core.inventory([str(tmp_path / "p")], {str(root.parent): {"my.coll": {"version": "1"}}})
    core.delete(copies[0])
    assert src.is_dir() and not (root / "coll").exists()


@pytest.fixture
def run(tree, monkeypatch, capsys):
    p1, p2 = tree
    monkeypatch.setattr(cli, "ansible_view",
                        lambda galaxy, extra: ([str(p1), str(p2)], listing(p1, p2)))

    def go(*argv):
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err
    return go


def test_cli_refuses_to_break_dependents(run, tree):
    code, out, err = run("demo.base", "--all", "-y")
    assert code == 1 and "depends on demo.base" in err
    assert (tree[0] / "ansible_collections" / "demo" / "base").is_dir()


def test_cli_removes_and_reports_new_active_copy(run, tree):
    code, out, err = run("demo.base", "--version", "1.0.0", "-y")
    assert code == 0, err
    assert "Afterwards Ansible loads demo.base 2.0.0" in out
    assert not (tree[0] / "ansible_collections" / "demo" / "base").exists()


def test_cli_dry_run_changes_nothing(run, tree):
    code, out, _ = run("demo.app", "--deps", "-n")
    assert code == 0 and "demo.base 1.0.0" in out
    assert (tree[0] / "ansible_collections" / "demo" / "app").is_dir()


def test_cli_needs_yes_without_terminal(run, tree, monkeypatch):
    monkeypatch.setattr("sys.stdin", open("/dev/null"))
    code, _, err = run("demo.other")
    assert code == 1 and "--yes" in err
    assert (tree[0] / "ansible_collections" / "demo" / "other").is_dir()


def test_cli_shadowed_and_list(run, tree):
    code, out, _ = run("--list")
    assert code == 0 and "shadowed by" in out and "[active]" in out
    code, out, _ = run("--shadowed", "-y")
    assert code == 0
    assert not (tree[1] / "ansible_collections" / "demo" / "base").exists()
    assert (tree[0] / "ansible_collections" / "demo" / "base").is_dir()
    assert run("--shadowed")[1] == "no shadowed copies\n"


def test_cli_usage_errors(run):
    with pytest.raises(SystemExit):
        run()
    with pytest.raises(SystemExit):
        run("--shadowed", "demo.app")


def test_no_existing_paths_on_old_ansible(monkeypatch):
    import subprocess

    def fake(cmd, **kw):
        if cmd[1] == "dump":
            return subprocess.CompletedProcess(cmd, 0, '[{"name": "COLLECTIONS_PATHS", "value": ["/x"]}]', "")
        raise subprocess.CalledProcessError(
            5, cmd, "", "ERROR! - None of the provided paths were usable. Please specify a valid path")
    monkeypatch.setattr(core.subprocess, "run", fake)
    assert core.ansible_view("ansible-galaxy", []) == (["/x"], {})


def test_other_listing_errors_are_reported(monkeypatch):
    import subprocess

    def fake(cmd, **kw):
        if cmd[1] == "dump":
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        raise subprocess.CalledProcessError(1, cmd, "", "ERROR! something else")
    monkeypatch.setattr(core.subprocess, "run", fake)
    with pytest.raises(core.Error, match="something else"):
        core.ansible_view("ansible-galaxy", [])
