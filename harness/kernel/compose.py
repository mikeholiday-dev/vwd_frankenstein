"""Composition: a capability calling other installed capabilities (manifest `uses`). Owner: A.

A capability lists what it calls in `uses`: `"name"` (the active version) or `"name@vN"` (pinned).
Its code calls them with `from frank import use` and `use("name", **args)`, which returns
that capability's `run(**args)` result. Nothing goes through the host: the used bundles are
copied next to the caller and run in the same container, under the caller's permissions.

So authority can't grow through `uses`: every capability reached (transitively) must need no
network host, secret or registry access that the caller's own manifest doesn't declare. The
gate refuses a bundle that breaks this, and the host re-checks on every call, because the
active version of a used capability can change (v2, rollback) or be quarantined after install.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from harness.contracts import CODE_FILE, TESTS_DIR, Manifest, Registry, RegistryEntry

USE_RE = re.compile(r"(?P<name>[a-z][a-z0-9_]{0,63})(?:@v(?P<version>[1-9][0-9]*))?")
USES_MODULE = "frank.py"  # `from frank import use`
USES_DIR = "_frank_uses"  # used bundles, one folder per name
RESERVED = (USES_MODULE, USES_DIR)  # the kernel writes these into the call/test dir; a bundle can't ship them

USE_SHIM = f'''\
"""Written by the harness: call another installed capability listed in this manifest's `uses`."""
import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).parent / "{USES_DIR}"


def use(name: str, **args):
    mod = sys.modules.get(f"{USES_DIR}.{{name}}")
    if mod is None:
        path = _DIR / name / "{CODE_FILE}"
        if not path.is_file():
            raise LookupError(f"{{name!r}} is not in this capability's `uses`")
        spec = importlib.util.spec_from_file_location(f"{USES_DIR}.{{name}}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    return mod.run(**args)
'''


class UsesError(ValueError):
    """`uses` can't be satisfied: unknown, quarantined, cyclic, or it would widen the caller's authority."""


def resolve(registry: Registry, manifest: Manifest) -> list[RegistryEntry]:
    """Every capability `manifest` reaches through `uses`, transitively, one entry per name."""
    found: dict[str, RegistryEntry] = {}

    def visit(m: Manifest, path: list[str]) -> None:
        if not isinstance(m.uses, list):
            raise UsesError(f"{m.ref}: `uses` must be a list")
        for spec in m.uses:
            match = USE_RE.fullmatch(spec) if isinstance(spec, str) else None
            if not match:
                raise UsesError(f"{m.ref}: bad `uses` entry {spec!r}, expected 'name' or 'name@vN'")
            name, version = match["name"], int(match["version"]) if match["version"] else None
            if name in path:
                raise UsesError(f"cycle in `uses`: {' -> '.join([*path, name])}")
            try:
                entry = registry.get(name, version)
            except (KeyError, FileNotFoundError):
                raise UsesError(f"{m.ref} uses {spec}, which isn't installed") from None
            if entry.status != "active":
                raise UsesError(f"{m.ref} uses {entry.manifest.ref}, which is {entry.status}")
            if name in found and found[name].manifest.version != entry.manifest.version:
                raise UsesError(f"{name} is reached at two versions: {found[name].manifest.ref} and {entry.manifest.ref}")
            found[name] = entry
            visit(entry.manifest, [*path, name])

    visit(manifest, [manifest.name])
    for entry in found.values():
        if reason := _wider(entry.manifest, manifest):
            raise UsesError(f"{manifest.ref} uses {entry.manifest.ref}, which needs {reason} that {manifest.ref} doesn't declare")
    return list(found.values())


def _wider(used: Manifest, caller: Manifest) -> str:
    p, q = used.permissions, caller.permissions
    if extra := sorted(set(p.network) - set(q.network)):
        return f"network {extra}"
    if extra := sorted(set(p.secrets) - set(q.secrets)):
        return f"secrets {extra}"
    if p.filesystem != "none" and p.filesystem != q.filesystem:
        return f"filesystem {p.filesystem!r}"
    return ""


def vendor(entries: list[RegistryEntry], work: Path) -> None:
    """Copy the used bundles (without their tests) and the `frank` module into `work`."""
    for entry in entries:
        shutil.copytree(entry.path, work / USES_DIR / entry.manifest.name, ignore=shutil.ignore_patterns(TESTS_DIR, ".git"))
    (work / USES_MODULE).write_text(USE_SHIM)


def dependencies(manifest: Manifest, entries: list[RegistryEntry]) -> list[str]:
    """The caller's deps plus those of everything it uses, deduplicated in order."""
    return list(dict.fromkeys([*manifest.dependencies, *(d for e in entries for d in e.manifest.dependencies)]))
