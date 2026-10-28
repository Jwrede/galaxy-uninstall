# galaxy-uninstall

Remove installed Ansible collections. `ansible-galaxy` can install collections but has no
command to remove them; `galaxy-uninstall` fills that gap with the checks you would otherwise
do by hand before running `rm -rf`:

- It asks Ansible where collections are (`ansible-config dump` for `COLLECTIONS_PATHS`,
  `ansible-galaxy collection list` for every installed copy, including `ansible_collections`
  in site-packages), so `ansible.cfg`, `ANSIBLE_COLLECTIONS_PATH` and `-p` are honoured.
- It shows which copy Ansible loads and which copies are shadowed by an earlier path.
- It refuses to remove a collection that another installed collection depends on
  (from `MANIFEST.json`) unless another copy stays installed.
- `--deps` also removes dependencies that no remaining collection needs.
- It will not delete collections that belong to a pip package (for example the ones bundled
  with the `ansible` package or ansible-core's own private collections), nor a directory that
  holds a `.git` checkout, and it unlinks symlinked collections without touching the target.
- It tells you which copy becomes active after the removal.

## Why

- [ansible/ansible#67759](https://github.com/ansible/ansible/issues/67759) "Add ansible-galaxy
  collection uninstall": 179 reactions (174 thumbs up), 20 comments, open since February 2020.
  The workaround in the thread is `rm -rf ~/.ansible/collections/ansible_collections/<ns>/<name>`,
  repeated for every place a copy might be. Users report ending up with
  [two versions of the same collection](https://github.com/ansible/ansible/issues/67759#issuecomment-1011297064)
  and [only knowing where one of them is](https://github.com/ansible/ansible/issues/67759#issuecomment-1954098889).
- Two upstream attempts were closed without merging:
  [#73464](https://github.com/ansible/ansible/pull/73464) (2021, closed 2022) and
  [#84738](https://github.com/ansible/ansible/pull/84738) (2025, closed March 2026; it removed
  only the requested collections, without dependency handling).
- Red Hat has a knowledge base article for the question,
  "[How to Uninstall an Ansible Collection](https://access.redhat.com/solutions/7101048)".
- [ansible-dev-environment](https://github.com/ansible/ansible-dev-environment) has an
  `ade uninstall` command, but it only works on the site-packages of the virtual environment it
  manages. It does not look at `~/.ansible/collections`, `/usr/share/ansible/collections` or
  project `collections/` directories and does not check dependencies.

## Installation

```sh
pipx install git+https://github.com/Jwrede/galaxy-uninstall
```

Python 3.9+, standard library only. It needs `ansible-galaxy` and `ansible-config` on `PATH`
(or `--ansible-galaxy /path/to/ansible-galaxy`); it does not import Ansible. Tested in CI with
ansible-core 2.15, 2.17 and the latest release.

## Example

A project whose `ansible.cfg` sets `collections_path = ./collections:~/.ansible/collections`,
with `demo.app` (depending on `demo.base`) installed in the project and other copies of
`demo.base` in the home directory and in the virtual environment. Output is from a real run,
with the paths shortened.

```console
$ galaxy-uninstall --list
demo.app   1.0.0  ~/proj/collections/ansible_collections  [active]
demo.base  1.0.0  ~/proj/collections/ansible_collections  [active]
demo.base  2.0.0  ~/.ansible/collections/ansible_collections  [shadowed by ~/proj/collections/ansible_collections]
demo.base  2.0.0  /venv/lib/python3.12/site-packages/ansible_collections  [shadowed by ~/proj/collections/ansible_collections]

$ galaxy-uninstall demo.base
galaxy-uninstall: demo.base is installed 3 times:
  demo.base 1.0.0 in ~/proj/collections/ansible_collections
  demo.base 2.0.0 in ~/.ansible/collections/ansible_collections
  demo.base 2.0.0 in /venv/lib/python3.12/site-packages/ansible_collections
Pick one with --path or --version, or pass --all.

$ galaxy-uninstall demo.base --all
refusing: demo.app 1.0.0 in ~/proj/collections/ansible_collections depends on demo.base
remove the dependents too, or pass --force

$ galaxy-uninstall demo.app --deps -n
Will remove:
  demo.app 1.0.0  ~/proj/collections/ansible_collections/demo/app
  demo.base 1.0.0  ~/proj/collections/ansible_collections/demo/base
Afterwards Ansible loads demo.base 2.0.0 in ~/.ansible/collections/ansible_collections

$ galaxy-uninstall --shadowed -y
Will remove:
  demo.base 2.0.0  ~/.ansible/collections/ansible_collections/demo/base
  demo.base 2.0.0  /venv/lib/python3.12/site-packages/ansible_collections/demo/base
removed ~/.ansible/collections/ansible_collections/demo/base
removed /venv/lib/python3.12/site-packages/ansible_collections/demo/base
```

Without `-y` it asks before deleting; when stdin is not a terminal it refuses unless `-y` is
given. `-n` only prints the plan.

## Options

| Option | Meaning |
|---|---|
| `NAMESPACE.COLLECTION ...` | collections to remove |
| `-p DIR` | only consider copies in `DIR` (repeatable); `DIR` is also searched, as with `ansible-galaxy collection list -p` |
| `--version V` | only the copy with version `V` |
| `--all` | every matching copy (otherwise more than one match is an error) |
| `--deps` | also remove dependencies that nothing remaining depends on |
| `--keep NAME` | never remove `NAME` as a dependency |
| `--force` | remove even if dependents remain or the directory is a git checkout |
| `--list [NAME ...]` | show installed copies and which one Ansible loads |
| `--shadowed` | remove copies that Ansible never loads because an earlier path has the same collection |
| `-n`, `-y` | dry run; no confirmation |

Removing a copy deletes `ansible_collections/<ns>/<name>`, the `<ns>.<name>-<version>.info`
directory that `ansible-galaxy` writes for Galaxy installs, and the namespace directory if it
is then empty.

## Limits

- The search order is the one `ansible` and `ansible-playbook` use outside a playbook
  directory: `COLLECTIONS_PATHS` in order, then site-packages. A `collections/` directory next
  to a playbook is searched first when that playbook runs; pass it with `-p` to see or remove
  copies there. Collections found only through `-p` are shown as "not on search path".
- Dependencies are read from `MANIFEST.json` (installed collections) or `galaxy.yml` (source
  checkouts, only if PyYAML is importable). Version ranges are not evaluated: a dependency
  counts as satisfied while any copy of it stays installed. Ansible itself does not check
  dependency versions when a playbook runs.
- `--deps` cannot know which collections you installed on purpose: ansible-galaxy keeps no
  record of that. A dependency you also use directly in playbooks is removed if no other
  collection needs it. Check with `-n` first, or protect it with `--keep`.
- Roles (`ansible-galaxy role remove` exists) and `requirements.yml` files are not handled.

## Development

```sh
python -m pip install -e . pytest ansible-core
pytest
```

`tests/test_ansible.py` builds collections with `ansible-galaxy`, installs them offline into
two paths, removes copies with `galaxy-uninstall` and checks with `ansible localhost` which
copy a filter plugin is loaded from. It is skipped when ansible-core is not installed.

## License

MIT
