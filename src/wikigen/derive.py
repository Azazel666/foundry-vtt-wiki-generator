"""Derived data: requirement closure, removal candidates, compatibility, drift. Pure functions of the inventory."""

import re
from dataclasses import dataclass, field
from functools import cmp_to_key

from packaging.version import InvalidVersion, Version

from .models import Compatibility, EnvInventory, Inventory, Package

SAME, NEWER, OLDER, MISSING, ONLY_HERE = "same", "newer", "older", "missing", "only here"
DEPRECATED, TOO_NEW, UNVERIFIED = "deprecated", "too new", "unverified"


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


def foundry_version(env: EnvInventory) -> str:
    """The Data folder doesn't record the server's version: the newest one a world was last opened with is the
    closest fact. Empty when no world has been opened."""
    return max((world.core_version for world in env.worlds.values()), key=cmp_to_key(compare_versions), default="")


def _generation(version: str) -> str:
    found = re.search(r"\d+", version)
    return found.group() if found else ""


def _past_maximum(maximum: str, running: str) -> bool:
    """A bare number such as 13 covers the whole generation, 13.351 included."""
    if maximum.isdigit():
        return compare_versions(_generation(running), maximum) > 0
    return compare_versions(running, maximum) > 0


def _past_verified(verified: str, running: str) -> bool:
    """Only a newer generation counts: nearly every package is a few builds behind, and that's no warning."""
    tested, current = _generation(verified), _generation(running)
    return bool(tested and current) and compare_versions(current, tested) > 0


def compat_level(compatibility: Compatibility, running: str) -> str | None:
    if compatibility.minimum and compare_versions(compatibility.minimum, running) > 0:
        return TOO_NEW
    if compatibility.maximum and _past_maximum(compatibility.maximum, running):
        return DEPRECATED
    if compatibility.verified and _past_verified(compatibility.verified, running):
        return UNVERIFIED
    return None


@dataclass
class CompatIssue:
    kind: str
    id: str
    title: str
    version: str
    level: str
    target: str  # "Foundry" or the game system's title
    running: str
    compatibility: Compatibility
    worlds: int


def _world_count(kind: str, package_id: str, env: EnvInventory) -> int:
    if kind == "module":
        return sum(package_id in world.enabled for world in env.worlds.values())
    return sum(package_id == world.system for world in env.worlds.values())


def compat_issues(env: EnvInventory) -> list[CompatIssue]:
    """Packages checked against the env's Foundry version, modules also against its installed game systems."""
    core = foundry_version(env)
    issues = []
    for kind, packages in (("system", env.systems), ("module", env.modules)):
        for package in packages.values():
            checks = [("Foundry", core, package.compatibility)] if core else []
            for system_id, compatibility in package.system_compatibility.items():
                system = env.systems.get(system_id)
                if system and system.version:
                    checks.append((system.name, system.version, compatibility))
            for target, running, compatibility in checks:
                level = compat_level(compatibility, running)
                if level:
                    issues.append(CompatIssue(kind, package.id, package.name, package.version, level, target, running,
                                              compatibility, _world_count(kind, package.id, env)))
    return sorted(issues, key=lambda issue: issue.title.lower())


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
