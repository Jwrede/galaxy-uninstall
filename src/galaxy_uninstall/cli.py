"""galaxy-uninstall: remove installed Ansible collections."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import (Error, active, ansible_view, blockers, broken, delete, inventory, root_of,
                   select, shadowed, with_orphans)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="galaxy-uninstall",
        description="Remove installed Ansible collections (the missing "
                    "'ansible-galaxy collection uninstall').")
    p.add_argument("names", nargs="*", metavar="NAMESPACE.COLLECTION")
    p.add_argument("-p", "--path", action="append", default=[], metavar="DIR",
                   help="only consider copies in DIR (repeatable); DIR is also searched "
                        "like 'ansible-galaxy collection list -p'")
    p.add_argument("--version", help="only remove the copy with this version")
    p.add_argument("--all", action="store_true", help="remove every matching copy")
    p.add_argument("--deps", action="store_true",
                   help="also remove dependencies that no remaining collection needs")
    p.add_argument("--keep", action="append", default=[], metavar="NAME",
                   help="never remove NAME as a dependency (repeatable)")
    p.add_argument("--force", action="store_true",
                   help="remove even if other collections depend on it or it is a git checkout")
    p.add_argument("--list", action="store_true",
                   help="show every installed copy and which one Ansible loads")
    p.add_argument("--shadowed", action="store_true",
                   help="remove copies that are never loaded because an earlier "
                        "path has the same collection")
    p.add_argument("-n", "--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--ansible-galaxy", default="ansible-galaxy", metavar="CMD",
                   help="ansible-galaxy executable to ask (default: from PATH); "
                        "ansible-config is taken from the same directory")
    p.add_argument("-V", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if args.list and args.shadowed or args.shadowed and args.names:
        p.error("--shadowed takes no names and cannot be combined with --list")
    if not (args.list or args.shadowed or args.names):
        p.error("give collection names, --list or --shadowed")
    return args


def status(c, act) -> str:
    if c.owner:
        return f"pip package {c.owner}"
    if c.rank is None:
        return "not on search path"
    return "active" if act.get(c.name) is c else f"shadowed by {act[c.name].root}"


def show_list(copies, names):
    act = active(copies)
    # ansible-core ships private collections such as ansible._protomatter
    rows = [c for c in copies if (c.name in names if names else not c.name.split(".")[1].startswith("_"))]
    if not rows:
        print("no matching collections installed")
        return
    width = max(len(c.name) for c in rows)
    vwidth = max(len(c.version) for c in rows)
    for c in rows:
        extra = "  (git checkout)" if c.checkout else ""
        print(f"{c.name:<{width}}  {c.version:<{vwidth}}  {c.root}  [{status(c, act)}]{extra}")


def confirm(args) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("not a terminal: pass --yes to remove", file=sys.stderr)
        return False
    return input("Remove? [y/N] ").strip().lower() in ("y", "yes")


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        copies = inventory(*ansible_view(args.ansible_galaxy, args.path))
        if args.list:
            show_list(copies, args.names)
            return 0
        if args.shadowed:
            roots = {root_of(p) for p in args.path}
            plan = [c for c in shadowed(copies)
                    if not c.owner and (not roots or c.root in roots)]
            if not plan:
                print("no shadowed copies")
                return 0
        else:
            plan = select(copies, args.names, args.path, args.version, args.all)
            if args.deps:
                plan = with_orphans(copies, plan, args.keep)
            checkouts = blockers(copies, plan)
            if checkouts and not args.force:
                for c in checkouts:
                    print(f"refusing: {c.path} is a git checkout (use --force)", file=sys.stderr)
                return 1
            hits = broken(copies, plan)
            if hits and not args.force:
                for c, dep in hits:
                    print(f"refusing: {c.label()} depends on {dep}", file=sys.stderr)
                print("remove the dependents too, or pass --force", file=sys.stderr)
                return 1
    except Error as exc:
        print(f"galaxy-uninstall: {exc}", file=sys.stderr)
        return 1

    before = active(copies)
    after = active([c for c in copies if c not in plan])
    print("Will remove:")
    for c in plan:
        print(f"  {c.name} {c.version}  {c.path}")
    for name in sorted({c.name for c in plan}):
        if before.get(name) is not after.get(name) and name in after:
            print(f"Afterwards Ansible loads {after[name].label()}")
    if args.force:
        for c, dep in broken(copies, plan):
            print(f"Warning: {c.label()} depends on {dep}, which will be gone")
    if args.dry_run or not confirm(args):
        return 0 if args.dry_run else 1
    for c in plan:
        delete(c)
        print(f"removed {c.path}")
    return 0
