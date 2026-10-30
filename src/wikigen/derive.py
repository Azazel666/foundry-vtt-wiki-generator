"""Derived data: requirement closure, removal candidates, drift. Pure functions of the inventory."""

import re
from dataclasses import dataclass, field

from packaging.version import InvalidVersion, Version

from .models import EnvInventory, Inventory, Package

SAME, NEWER, OLDER, MISSING, ONLY_HERE = "same", "newer", "older", "missing", "only here"


def _natural(value: str) -> tuple:
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.lower())
                 for part in re.findall(r"\d+|[^\d.+_-]+", value))


def compare_versions(left: str, right: str) -> int:
    """-1, 0 or 1. Never raises: strings that are not PEP 440 versions are compared piece by piece."""
    if left == right:
        return 0
    try:
        a, b = Version(left), Version(right)
    except InvalidVersion:
        a, b = _natural(left), _natural(right)
    return (a > b) - (a < b)


def newest(packages: list[Package]) -> Package:
    best = packages[0]
    for package in packages[1:]:
        if compare_versions(package.version, best.version) > 0:
            best = package
    return best


def requirement_closure(enabled: list[str], modules: dict[str, Package]) -> set[str]:
    """Everything the enabled modules need, transitively (excluding what isn't installed)."""
    seen: set[str] = set()
    stack = list(enabled)
    while stack:
        package = modules.get(stack.pop())
        if package is None:
            continue
        for requirement in package.requires:
            if requirement not in seen:
                seen.add(requirement)
                stack.append(requirement)
    return seen


@dataclass
class EnvFindings:
    removal_candidates: list[str] = field(default_factory=list)
    unused_libraries: list[str] = field(default_factory=list)
    unused_systems: list[str] = field(default_factory=list)


def env_findings(env: EnvInventory) -> EnvFindings:
    findings = EnvFindings()
    needed: set[str] = set()
    for world in env.worlds.values():
        system = env.systems.get(world.system)
        roots = world.enabled + (system.requires if system else [])
        needed |= set(roots) | requirement_closure(roots, env.modules)
    for module_id, module in env.modules.items():
        if module_id not in needed:
            (findings.unused_libraries if module.library else findings.removal_candidates).append(module_id)
    used_systems = {world.system for world in env.worlds.values()}
    findings.unused_systems = [system for system in env.systems if system not in used_systems]
    return findings


@dataclass
class DriftRow:
    kind: str
    id: str
    title: str
    versions: dict[str, str]  # env -> version; absent when not installed
    states: dict[str, str]  # env -> SAME, NEWER, OLDER, MISSING or ONLY_HERE, against the reference


def drift_state(reference: str | None, version: str | None) -> str | None:
    if version is None:
        return MISSING if reference is not None else None
    if reference is None:
        return ONLY_HERE
    order = compare_versions(version, reference)
    return NEWER if order > 0 else OLDER if order < 0 else SAME


def drift(inventory: Inventory, reference_env: str) -> list[DriftRow]:
    """Packages that differ from the reference env; those identical everywhere are left out."""
    rows = []
    for kind, section in (("system", "systems"), ("module", "modules")):
        titles: dict[str, str] = {}
        for env in inventory.envs.values():
            for package_id, package in getattr(env, section).items():
                titles.setdefault(package_id, package.name)
        reference = getattr(inventory.envs[reference_env], section) if reference_env in inventory.envs else {}
        for package_id in sorted(titles, key=lambda key: titles[key].lower()):
            base = reference[package_id].version if package_id in reference else None
            versions, states = {}, {}
            for name, env in inventory.envs.items():
                package = getattr(env, section).get(package_id)
                version = package.version if package else None
                if version is not None:
                    versions[name] = version
                state = drift_state(base, version)
                if state and name != reference_env:
                    states[name] = state
            if any(state != SAME for state in states.values()):
                rows.append(DriftRow(kind, package_id, titles[package_id], versions, states))
    return rows
