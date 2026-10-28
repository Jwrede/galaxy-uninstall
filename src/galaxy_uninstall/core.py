"""Find installed Ansible collections and plan their removal.

Discovery asks Ansible itself: ``ansible-config dump`` gives the configured
COLLECTIONS_PATHS in search order and ``ansible-galaxy collection list`` gives
every collection directory it can see, including ``ansible_collections`` in
site-packages, which Ansible searches after the configured paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class Error(Exception):
    pass


@dataclass
class Copy:
    """One installed copy of a collection."""

    name: str  # namespace.collection
    version: str
    root: Path  # the .../ansible_collections directory
    rank: int | None  # search order; None = not on Ansible's search path
    deps: dict = field(default_factory=dict)
    owner: str | None = None  # pip distribution that installed it
    checkout: bool = False  # directory holds a .git

    @property
    def path(self) -> Path:
        namespace, coll = self.name.split(".", 1)
        return self.root / namespace / coll

    def label(self) -> str:
        return f"{self.name} {self.version} in {self.root}"


def _run_json(cmd: list[str]):
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise Error(f"{cmd[0]} not found on PATH (is ansible-core installed?)")
    except subprocess.CalledProcessError as exc:
        raise Error(f"{' '.join(cmd)} failed:\n{exc.stderr.strip()}")
    try:
        return json.loads(out)
    except ValueError:
        raise Error(f"{' '.join(cmd)} did not print JSON:\n{out[:500]}")


def ansible_view(galaxy: str, extra_paths: list[str]) -> tuple[list[str], dict]:
    """Return (configured COLLECTIONS_PATHS, ansible-galaxy collection list JSON)."""
    config_bin = str(Path(galaxy).with_name("ansible-config")) if os.sep in galaxy else "ansible-config"
    dump = _run_json([config_bin, "dump", "--format", "json"])
    configured: list[str] = []
    for item in dump:
        if item.get("name") == "COLLECTIONS_PATHS":
            value = item.get("value") or []
            configured = value.split(os.pathsep) if isinstance(value, str) else list(value)
    cmd = [galaxy, "collection", "list", "--format", "json"]
    for p in extra_paths:
        cmd += ["-p", p]
    return configured, _run_json(cmd)


def _norm(p: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def root_of(p: str | Path) -> Path:
    """Map a configured collections path to its ansible_collections directory."""
    p = _norm(p)
    return p if p.name == "ansible_collections" else p / "ansible_collections"


def read_deps(path: Path) -> dict:
    manifest = path / "MANIFEST.json"
    if manifest.is_file():
        try:
            info = json.loads(manifest.read_text()).get("collection_info") or {}
        except ValueError:
            return {}
        return dict(info.get("dependencies") or {})
    galaxy_yml = path / "galaxy.yml"
    if galaxy_yml.is_file():
        try:
            import yaml  # PyYAML ships with ansible-core, maybe not in our env
        except ImportError:
            return {}
        data = yaml.safe_load(galaxy_yml.read_text()) or {}
        return dict(data.get("dependencies") or {})
    return {}


_record_cache: dict[Path, list[tuple[str, set[str]]]] = {}


def pip_owner(path: Path) -> str | None:
    """Name of the pip distribution whose RECORD lists files under path."""
    for parent in path.parents:
        records = _record_cache.get(parent)
        if records is None:
            records = []
            for rec in sorted(parent.glob("*.dist-info/RECORD")):
                dirs = set()
                for line in rec.read_text(errors="replace").splitlines():
                    entry = line.split(",", 1)[0]
                    head = entry.rsplit("/", 1)[0]
                    while head and head not in dirs:
                        dirs.add(head)
                        head = head.rpartition("/")[0]
                records.append((rec.parent.name.split("-")[0], dirs))
            _record_cache[parent] = records
        if records:
            rel = path.relative_to(parent).as_posix()
            for dist, dirs in records:
                if rel in dirs:
                    return dist
            return None
    return None


def inventory(configured: list[str], listing: dict) -> list[Copy]:
    """Build the list of installed copies, sorted by Ansible's search order."""
    order = []
    for p in configured:
        r = root_of(p)
        if r not in order:
            order.append(r)
    copies = []
    extra_rank = len(order)
    for root_str in listing:
        root = _norm(root_str)
        if root in order:
            rank = order.index(root)
        elif "site-packages" in root.parts or "dist-packages" in root.parts:
            rank = extra_rank
            extra_rank += 1
        else:
            rank = None  # only reachable through -p
        for name, meta in listing[root_str].items():
            c = Copy(name=name, version=str((meta or {}).get("version", "*")), root=root, rank=rank)
            c.deps = read_deps(c.path)
            c.owner = pip_owner(c.path)
            c.checkout = (c.path / ".git").exists()
            copies.append(c)
    copies.sort(key=lambda c: (c.name, c.rank is None, c.rank or 0))
    return copies


def active(copies: list[Copy]) -> dict[str, Copy]:
    """The copy Ansible loads for each name: first on the search path."""
    found: dict[str, Copy] = {}
    for c in copies:
        if c.rank is not None and c.name not in found:
            found[c.name] = c
    return found


def shadowed(copies: list[Copy]) -> list[Copy]:
    act = active(copies)
    return [c for c in copies if c.rank is not None and act[c.name] is not c]


def select(copies: list[Copy], names: list[str], paths: list[str], version: str | None,
           all_copies: bool) -> list[Copy]:
    roots = {root_of(p) for p in paths}
    chosen = []
    for name in names:
        if name.count(".") != 1:
            raise Error(f"{name}: expected namespace.collection")
        matches = [c for c in copies if c.name == name]
        if not matches:
            raise Error(f"{name} is not installed")
        if roots:
            matches = [c for c in matches if c.root in roots]
        if version:
            matches = [c for c in matches if c.version == version]
        if not matches:
            raise Error(f"{name}: no installed copy matches the given --path/--version")
        if len(matches) > 1 and not all_copies:
            lines = "\n".join("  " + c.label() for c in matches)
            raise Error(f"{name} is installed {len(matches)} times:\n{lines}\n"
                        "Pick one with --path or --version, or pass --all.")
        chosen += matches
    return chosen


def blockers(copies: list[Copy], remove: list[Copy]) -> list[Copy]:
    for c in remove:
        if c.owner:
            raise Error(f"{c.label()} belongs to the pip package '{c.owner}'; "
                        f"remove it with pip, not by deleting files")
    return [c for c in remove if c.checkout]


def broken(copies: list[Copy], remove: list[Copy]) -> list[tuple[Copy, str]]:
    """Remaining collections whose dependency would have no copy left."""
    gone = {id(c) for c in remove}
    left = [c for c in copies if id(c) not in gone]
    left_names = {c.name for c in left}
    removed_names = {c.name for c in remove}
    return [(c, dep) for c in left for dep in c.deps
            if dep in removed_names and dep not in left_names]


def with_orphans(copies: list[Copy], remove: list[Copy], keep: list[str]) -> list[Copy]:
    """Add dependencies of removed copies that nothing left depends on.

    Only copies in the same directory as the collection that pulled them in
    are considered, since ansible-galaxy installs dependencies next to it.
    """
    plan = list(remove)
    while True:
        gone = {id(c) for c in plan}
        needed = {d for c in copies if id(c) not in gone for d in c.deps}
        added = False
        for c in list(plan):
            for dep in c.deps:
                if dep in needed or dep in keep:
                    continue
                for d in copies:
                    if (d.name == dep and d.root == c.root and id(d) not in gone
                            and not d.owner and not d.checkout):
                        plan.append(d)
                        gone.add(id(d))
                        added = True
        if not added:
            return plan


def delete(c: Copy) -> list[Path]:
    """Remove one copy, its .info directory and an empty namespace directory."""
    removed = []
    if c.path.is_symlink():
        c.path.unlink()
    else:
        shutil.rmtree(c.path)
    removed.append(c.path)
    for info in c.root.glob(f"{c.name}-*.info"):
        if info.is_dir():
            shutil.rmtree(info)
            removed.append(info)
    ns_dir = c.path.parent
    try:
        ns_dir.rmdir()
        removed.append(ns_dir)
    except OSError:
        pass
    return removed
